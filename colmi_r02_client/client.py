import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
import logging
from pathlib import Path
from types import TracebackType
from typing import Any, Optional
import json
import sys
import threading
import time

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
import paho.mqtt.client as mqtt
import serial
import serial.tools.list_ports

from colmi_r02_client import battery, date_utils, steps, set_time, blink_twice, hr, hr_settings, packet, reboot, real_time
from colmi_r02_client.visualization import SensorDataVisualizer

# Try to import Raspberry Pi sensor libraries (optional)
try:
    import board
    import busio
    import adafruit_mpu6050
    import adafruit_bme280
    # For analog sensors, try ADS1115 ADC
    try:
        import adafruit_ads1x15.ads1115 as ADS
        from adafruit_ads1x15.analog_in import AnalogIn
        ADS1115_AVAILABLE = True
    except ImportError:
        ADS1115_AVAILABLE = False
    RASPBERRY_PI_SENSORS_AVAILABLE = True
except ImportError:
    RASPBERRY_PI_SENSORS_AVAILABLE = False
    ADS1115_AVAILABLE = False

UART_SERVICE_UUID = "6E40FFF0-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
UART_TX_CHAR_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"

DEVICE_INFO_UUID = "0000180A-0000-1000-8000-00805F9B34FB"
DEVICE_HW_UUID = "00002A27-0000-1000-8000-00805F9B34FB"
DEVICE_FW_UUID = "00002A26-0000-1000-8000-00805F9B34FB"

logger = logging.getLogger(__name__)


def empty_parse(_packet: bytearray) -> None:
    """Used for commands that we expect a response, but there's nothing in the response"""
    return None


def log_packet(packet: bytearray) -> None:
    print("received: ", packet)


# TODO move this maybe?
@dataclass
class FullData:
    address: str
    heart_rates: list[hr.HeartRateLog | hr.NoData]
    sport_details: list[list[steps.SportDetail] | steps.NoData]


COMMAND_HANDLERS: dict[int, Callable[[bytearray], Any]] = {
    battery.CMD_BATTERY: battery.parse_battery,
    real_time.CMD_START_REAL_TIME: real_time.parse_real_time_reading,
    real_time.CMD_STOP_REAL_TIME: empty_parse,
    steps.CMD_GET_STEP_SOMEDAY: steps.SportDetailParser().parse,
    hr.CMD_READ_HEART_RATE: hr.HeartRateLogParser().parse,
    set_time.CMD_SET_TIME: empty_parse,
    hr_settings.CMD_HEART_RATE_LOG_SETTINGS: hr_settings.parse_heart_rate_log_settings,
}
"""
TODO put these somewhere nice

These are commands that we expect to have a response returned for
they must accept a packet as bytearray and then return a value to be put
in the queue for that command type
NOTE: if the value returned is None, it is not added to the queue, this is to support
multi packet messages where the parser has state
"""


class Client:
    def __init__(self, address: str, record_to: Path | None = None, use_mqtt: bool = True, 
                 serial_port: Optional[str] = "COM4", baud_rate: int = 115200,
                 use_visualization: bool = True, use_raspberry_pi_sensors: bool = False,
                 sensor_reporting_period_ms: int = 5000, i2c_sda_pin: int = 21, i2c_scl_pin: int = 22,
                 ads1115_address: int = 0x48, sound_adc_channel: int = 0, light_adc_channel: int = 1):
        self.address = address
        self.bleak_client = BleakClient(self.address)
        self.queues: dict[int, asyncio.Queue] = {cmd: asyncio.Queue() for cmd in COMMAND_HANDLERS}
        self.record_to = record_to
        
        # Track the latest heart rate reading
        self.latest_heart_rate = None
        
        # Raspberry Pi sensor setup
        self.use_raspberry_pi_sensors = use_raspberry_pi_sensors
        self.sensor_reporting_period_ms = sensor_reporting_period_ms
        self.sensor_task = None
        self.sensor_stopping = False
        
        # Sensor objects
        self.mpu = None
        self.bme = None
        self.i2c = None
        self.ads = None
        self.sound_channel = None
        self.light_channel = None
        
        # Sensor data tracking
        self.prev_accel_x = 0.0
        self.prev_accel_y = 0.0
        self.prev_accel_z = 0.0
        
        # Sound intensity tracking (similar to Arduino code)
        self.sound_min = 4095
        self.sound_max = 0
        self.sound_intensity = 0
        self.intensity_values = [0] * 10  # INTENSITY_AVG_COUNT = 10
        self.intensity_index = 0
        self.avg_sound_intensity = 0
        self.ts_sound_window = 0
        self.SOUND_WINDOW_MS = 20  # 20 milliseconds window
        
        if use_raspberry_pi_sensors:
            self._init_raspberry_pi_sensors(i2c_sda_pin, i2c_scl_pin, ads1115_address, sound_adc_channel, light_adc_channel)
        
        # Serial port setup
        self.serial_port = serial_port
        self.serial_conn = None
        if serial_port:
            try:
                self.serial_conn = serial.Serial(serial_port, baud_rate, timeout=1)
                logger.info(f"Connected to serial port {serial_port} at {baud_rate} baud")
                print(f"Connected to serial port {serial_port} at {baud_rate} baud")
            except serial.SerialException as e:
                logger.error(f"Failed to connect to serial port {serial_port}: {e}")
                print(f"Failed to connect to serial port {serial_port}: {e}")
        
        # Visualization setup
        self.use_visualization = use_visualization
        self.visualizer = None
        if use_visualization:
            # Create the visualizer but don't start it yet
            self.visualizer = SensorDataVisualizer()
            print("Created visualization instance")
            logger.info("Created visualization instance")
        else:
            print("Visualization disabled")
            logger.info("Visualization disabled")

        # MQTT setup
        self.use_mqtt = use_mqtt
        if use_mqtt:
            self.mqtt_client = mqtt.Client()
            
            # Define MQTT callbacks
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            
            # Connect to the broker
            self.mqtt_client.connect("server.nikolaacademy.com", 1883, 60)
            self.mqtt_client.loop_start()
            logger.info("Connected to MQTT broker")
            
            # Start sensor reading task if enabled
            if self.use_raspberry_pi_sensors:
                self._start_sensor_reading_task()

    def _start_visualization(self):
        """Start the visualization in the main thread."""
        if self.visualizer:
            logger.info("Starting visualization in main thread")
            self.visualizer.start()
            logger.info("Visualization window closed")
            
    def start_visualization_nonblocking(self):
        """
        Start visualization in a separate thread.
        Note: This might cause issues as matplotlib prefers to run in the main thread.
        """
        if self.visualizer and not self.visualizer.running:
            logger.info("Starting visualization in background thread")
            print("Starting visualization in background thread")
            self.viz_thread = threading.Thread(target=self._start_visualization)
            self.viz_thread.daemon = True
            self.viz_thread.start()
            logger.info("Visualization thread started")
    
    def _init_raspberry_pi_sensors(self, i2c_sda_pin: int, i2c_scl_pin: int, 
                                    ads1115_address: int, sound_adc_channel: int, light_adc_channel: int):
        """Initialize Raspberry Pi sensors (MPU6050, BME280, and ADC for analog sensors)."""
        if not RASPBERRY_PI_SENSORS_AVAILABLE:
            logger.warning("Raspberry Pi sensor libraries not available. Install: adafruit-circuitpython-mpu6050 adafruit-circuitpython-bme280 adafruit-circuitpython-ads1x15")
            print("Raspberry Pi sensor libraries not available. Install required packages.")
            self.use_raspberry_pi_sensors = False
            return
        
        try:
            # Initialize I2C bus
            self.i2c = busio.I2C(board.SCL, board.SDA)
            logger.info(f"Initialized I2C bus (SDA: {i2c_sda_pin}, SCL: {i2c_scl_pin})")
            
            # Initialize MPU6050
            try:
                self.mpu = adafruit_mpu6050.MPU6050(self.i2c)
                self.mpu.accelerometer_range = adafruit_mpu6050.Range.RANGE_8_G
                self.mpu.gyro_range = adafruit_mpu6050.GyroRange.RANGE_500_DEG
                self.mpu.filter_bandwidth = adafruit_mpu6050.Bandwidth.BAND_21_HZ
                logger.info("MPU6050 initialized successfully")
                print("MPU6050 initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize MPU6050: {e}")
                print(f"Failed to initialize MPU6050: {e}")
                self.mpu = None
            
            # Initialize BME280
            try:
                self.bme = adafruit_bme280.Adafruit_BME280_I2C(self.i2c, address=0x76)
                logger.info("BME280 initialized successfully")
                print("BME280 initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize BME280: {e}")
                print(f"Failed to initialize BME280: {e}")
                self.bme = None
            
            # Initialize ADS1115 for analog sensors (sound and light)
            if ADS1115_AVAILABLE:
                try:
                    self.ads = ADS.ADS1115(self.i2c, address=ads1115_address)
                    # Map channel numbers to ADS1115 pin constants
                    channel_map = {0: ADS.P0, 1: ADS.P1, 2: ADS.P2, 3: ADS.P3}
                    if sound_adc_channel not in channel_map or light_adc_channel not in channel_map:
                        raise ValueError(f"Invalid ADC channel. Must be 0-3, got sound={sound_adc_channel}, light={light_adc_channel}")
                    self.sound_channel = AnalogIn(self.ads, channel_map[sound_adc_channel])
                    self.light_channel = AnalogIn(self.ads, channel_map[light_adc_channel])
                    logger.info(f"ADS1115 initialized successfully (address: 0x{ads1115_address:02x}, sound=ch{sound_adc_channel}, light=ch{light_adc_channel})")
                    print(f"ADS1115 initialized successfully (address: 0x{ads1115_address:02x}, sound=ch{sound_adc_channel}, light=ch{light_adc_channel})")
                except Exception as e:
                    logger.error(f"Failed to initialize ADS1115: {e}")
                    print(f"Failed to initialize ADS1115: {e}")
                    self.ads = None
            else:
                logger.warning("ADS1115 library not available. Analog sensors (sound/light) will not work.")
                print("ADS1115 library not available. Install adafruit-circuitpython-ads1x15 for analog sensors.")
        except Exception as e:
            logger.error(f"Failed to initialize Raspberry Pi sensors: {e}")
            print(f"Failed to initialize Raspberry Pi sensors: {e}")
            self.use_raspberry_pi_sensors = False
    
    def _read_sensors(self) -> dict[str, Any]:
        """Read all sensor values and return as dictionary."""
        sensor_data = {}
        
        # Read MPU6050 (accelerometer/gyroscope)
        if self.mpu:
            try:
                accel = self.mpu.acceleration
                gyro = self.mpu.gyro
                temp_mpu = self.mpu.temperature
                
                # Calculate movement (change in acceleration)
                delta_x = abs(accel[0] - self.prev_accel_x)
                delta_y = abs(accel[1] - self.prev_accel_y)
                delta_z = abs(accel[2] - self.prev_accel_z)
                
                movement_value = max(delta_x, max(delta_y, delta_z))
                
                # Update previous acceleration values
                self.prev_accel_x = accel[0]
                self.prev_accel_y = accel[1]
                self.prev_accel_z = accel[2]
                
                sensor_data["movement"] = round(movement_value, 2)
            except Exception as e:
                logger.error(f"Error reading MPU6050: {e}")
        
        # Read BME280 (temperature, humidity, pressure)
        if self.bme:
            try:
                sensor_data["temperature"] = round(self.bme.temperature, 2)
                sensor_data["humidity"] = round(self.bme.relative_humidity, 2)
                # Note: pressure is available but not in the original Arduino payload
            except Exception as e:
                logger.error(f"Error reading BME280: {e}")
        
        # Read analog sensors (sound and light) via ADS1115
        if self.ads and self.sound_channel and self.light_channel:
            try:
                # Read light value
                light_value = int(self.light_channel.value)
                sensor_data["light"] = light_value
                
                # Sound intensity is calculated continuously in the sensor loop
                # Just use the current averaged value
                sensor_data["sound"] = self.avg_sound_intensity
            except Exception as e:
                logger.error(f"Error reading analog sensors: {e}")
        
        return sensor_data
    
    def _start_sensor_reading_task(self):
        """Start the sensor reading task in a background thread."""
        if self.sensor_task is not None:
            logger.warning("Sensor reading task already running")
            return
        
        self.sensor_stopping = False
        
        def sensor_loop():
            """Background thread loop for reading sensors and publishing to MQTT."""
            logger.info("Starting sensor reading task")
            ts_last_report = 0
            
            while not self.sensor_stopping:
                try:
                    current_time_ms = int(time.time() * 1000)
                    
                    # Continuously monitor sound levels (similar to Arduino code)
                    if self.ads and self.sound_channel:
                        try:
                            current_sound_value = int(self.sound_channel.value)
                            
                            # Update min/max sound values
                            if current_sound_value < self.sound_min:
                                self.sound_min = current_sound_value
                            if current_sound_value > self.sound_max:
                                self.sound_max = current_sound_value
                            
                            # Reset the min/max values periodically and calculate intensity
                            if current_time_ms - self.ts_sound_window > self.SOUND_WINDOW_MS:
                                # Calculate sound intensity as the range between min and max
                                self.sound_intensity = self.sound_max - self.sound_min
                                
                                # Add to moving average array
                                self.intensity_values[self.intensity_index] = self.sound_intensity
                                self.intensity_index = (self.intensity_index + 1) % len(self.intensity_values)
                                
                                # Calculate the average intensity
                                avg_sum = sum(self.intensity_values)
                                self.avg_sound_intensity = avg_sum // len(self.intensity_values)
                                
                                self.ts_sound_window = current_time_ms
                                
                                # Reset min/max for next window
                                self.sound_min = 32767  # Max value for 16-bit ADC
                                self.sound_max = 0
                        except Exception as e:
                            logger.debug(f"Error monitoring sound: {e}")
                    
                    # Read sensors and publish at the specified interval
                    if current_time_ms - ts_last_report >= self.sensor_reporting_period_ms:
                        sensor_data = self._read_sensors()
                        
                        if sensor_data and self.use_mqtt and self.mqtt_client.is_connected():
                            # Prepare JSON payload (matching Arduino format)
                            payload = json.dumps(sensor_data)
                            
                            # Publish to MQTT
                            try:
                                self.mqtt_client.publish("minori-blanket-sensors", payload)
                                logger.debug(f"Published sensor data: {payload}")
                                
                                # Print sensor readings
                                print("\n=== SENSOR READINGS ===")
                                for key, value in sensor_data.items():
                                    print(f"  {key}: {value}")
                                print("========================\n")
                            except Exception as e:
                                logger.error(f"Failed to publish sensor data: {e}")
                        
                        ts_last_report = current_time_ms
                    
                    # Small sleep to prevent CPU spinning
                    time.sleep(0.01)  # 10ms sleep for better sound monitoring resolution
                except Exception as e:
                    logger.error(f"Error in sensor reading loop: {e}")
                    time.sleep(1)
            
            logger.info("Sensor reading task stopped")
        
        self.sensor_task = threading.Thread(target=sensor_loop)
        self.sensor_task.daemon = True
        self.sensor_task.start()
        logger.info("Sensor reading task started")

    # MQTT callback for when the client connects to the broker
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        logger.info(f"Connected to MQTT broker with result code {rc}")
        # Subscribe to topics (including the blanket sensors topic)
        client.subscribe("minori-blanket-sensors")
        logger.info("Subscribed to minori-blanket-sensors topic")
    
    # MQTT callback for when a message is received
    def _on_mqtt_message(self, client, userdata, msg):
        try:
            payload = msg.payload.decode()
            logger.info(f"Received MQTT message from topic {msg.topic}: {payload}")
            
            # Add heart rate to blanket sensor data if available
            if msg.topic == "minori-blanket-sensors":
                self.start_visualization_nonblocking()
                try:
                    # Parse the JSON
                    data = json.loads(payload)
                    
                    # Add heart rate to the data if available
                    if self.latest_heart_rate is not None:
                        data["heartrate"] = self.latest_heart_rate
                        # Convert back to JSON for serial and logging
                        enhanced_payload = json.dumps(data)
                        logger.info(f"Added heart rate to blanket sensor data: {enhanced_payload}")
                        print(f"Added heart rate to blanket sensor data: {enhanced_payload}")
                    else:
                        logger.info("No heart rate data available yet")
                    
                    # Update visualization regardless of heart rate availability
                    if self.visualizer:
                        # Ensure all values are properly converted to float
                        cleaned_data = {}
                        for key, value in data.items():
                            try:
                                cleaned_data[key] = float(value)
                            except (ValueError, TypeError):
                                logger.warning(f"Could not convert {key}={value} to float")
                                # Skip this value
                        
                        logger.info(f"Updating visualization with data: {cleaned_data}")
                        self.visualizer.update_data(cleaned_data)
                        print(f"Updated visualization with data points: {list(cleaned_data.keys())}")
                    
                    # Send to serial port if connected
                    if self.serial_conn and self.serial_conn.is_open:
                        try:
                            serial_payload = json.dumps(data) if self.latest_heart_rate is None else enhanced_payload
                            self.serial_conn.write((serial_payload + '\n').encode())
                            logger.debug(f"Sent to serial port: {serial_payload}")
                        except Exception as e:
                            logger.error(f"Failed to send to serial port: {e}")
                    
                    # Format display based on the topic
                    print("\n===== SENSOR DATA =====")
                    for key, value in data.items():
                        print(f"  {key}: {value}")
                    print("=======================\n")
                    
                    # Return early since we've handled the message
                    return
                except json.JSONDecodeError:
                    # If not JSON, fall back to standard handling
                    logger.warning(f"Failed to parse blanket sensor data as JSON: {payload}")
            
            # Handle other topics or non-JSON data
            print(f"MQTT [{msg.topic}]: {payload}")
        except Exception as e:
            logger.error(f"Error processing MQTT message: {e}")
            import traceback
            traceback.print_exc()

    async def __aenter__(self) -> "Client":
        logger.info(f"Connecting to {self.address}")
        await self.connect()
        logger.info("Connected!")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        logger.info("Disconnecting")
        if exc_val is not None:
            logger.error("had an error")
        
        # Stop sensor reading task if enabled
        if self.use_raspberry_pi_sensors and self.sensor_task:
            self.sensor_stopping = True
            if self.sensor_task.is_alive():
                self.sensor_task.join(timeout=2)
            logger.info("Stopped sensor reading task")
        
        # Disconnect MQTT if enabled
        if self.use_mqtt:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("Disconnected from MQTT broker")
        
        # Stop visualization if enabled
        if self.visualizer:
            self.visualizer.stop()
            logger.info("Stopped visualization")
        
        # Close serial port if open
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            logger.info("Closed serial port connection")
        
        # Clean up I2C resources
        if self.i2c:
            try:
                self.i2c.deinit()
            except Exception:
                pass
            
        await self.disconnect()

    async def connect(self):
        await self.bleak_client.connect()

        nrf_uart_service = self.bleak_client.services.get_service(UART_SERVICE_UUID)
        assert nrf_uart_service
        rx_char = nrf_uart_service.get_characteristic(UART_RX_CHAR_UUID)
        assert rx_char
        self.rx_char = rx_char

        await self.bleak_client.start_notify(UART_TX_CHAR_UUID, self._handle_tx)

    async def disconnect(self):
        await self.bleak_client.disconnect()

    def _handle_tx(self, _: BleakGATTCharacteristic, packet: bytearray) -> None:
        """Bleak callback that handles new packets from the ring."""

        logger.info(f"Received packet {packet}")

        assert len(packet) == 16, f"Packet is the wrong length {packet}"
        packet_type = packet[0]
        assert packet_type < 127, f"Packet has error bit set {packet}"

        if packet_type in COMMAND_HANDLERS:
            result = COMMAND_HANDLERS[packet_type](packet)
            if result is not None:
                self.queues[packet_type].put_nowait(result)
            else:
                logger.debug(f"No result returned from parser for {packet_type}")
        else:
            logger.warning(f"Did not expect this packet: {packet}")

        if self.record_to is not None:
            with self.record_to.open("ab") as f:
                f.write(packet)
                f.write(b"\n")

    async def send_packet(self, packet: bytearray) -> None:
        logger.debug(f"Sending packet: {packet}")
        logger.debug(f"Packet type: {packet[0]}")
        print(f"Sending packet: {packet}")
        print(f"Packet type: {packet[0]}")

        await self.bleak_client.write_gatt_char(self.rx_char, packet, response=False)

    async def get_battery(self) -> battery.BatteryInfo:
        await self.send_packet(battery.BATTERY_PACKET)
        result = await self.queues[battery.CMD_BATTERY].get()
        assert isinstance(result, battery.BatteryInfo)
        return result

    async def _poll_real_time_reading(self, reading_type: real_time.RealTimeReading) -> None:
        import asyncio
        import threading
        
        start_packet = real_time.get_start_packet(reading_type)
        stop_packet = real_time.get_stop_packet(reading_type)

        await self.send_packet(start_packet)

        # Comment out list pooling - we'll just return None at the end
        # valid_readings: list[int] = []
        valid_readings = None  # Just for compatibility with return type
        error = False
        stopping = False
        
        # Use a separate thread for keyboard monitoring to not block asyncio
        def check_for_quit():
            nonlocal stopping
            print("Press 'q' to stop collecting readings...")
            while not stopping:
                if sys.platform == 'win32':
                    import msvcrt
                    if msvcrt.kbhit() and msvcrt.getch() == b'q':
                        print("Stopping data collection...")
                        stopping = True
                        break
                else:
                    # For Unix-like systems
                    import select
                    rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
                    if rlist and sys.stdin.read(1) == 'q':
                        print("Stopping data collection...")
                        stopping = True
                        break
                    
        # Start keyboard monitoring in separate thread
        keyboard_thread = threading.Thread(target=check_for_quit)
        keyboard_thread.daemon = True  # Thread will exit when main program exits
        keyboard_thread.start()
        
        try:
            data_count = 0
            last_data_time = asyncio.get_event_loop().time()
            
            while not stopping:
                try:
                    # Resend start packet every 10 readings or if no data for 5 seconds
                    current_time = asyncio.get_event_loop().time()
                    print(f"Current time: {current_time}, last data time: {last_data_time}, data count: {data_count}")
                    if data_count >= 10 or (data_count >= 10 and (current_time - last_data_time > 5)):
                        logger.debug(f"Sending packet: {start_packet}")
                        logger.debug(f"Packet type: {start_packet.decode('utf-8')}")
                        print(f"Sending packet: {start_packet}")
                        print(f"Packet type: {start_packet.decode('utf-8')}")
                        await self.send_packet(start_packet)
                        data_count = 0
                        last_data_time = current_time
                    
                    data: real_time.Reading | real_time.ReadingError = await asyncio.wait_for(
                        self.queues[real_time.CMD_START_REAL_TIME].get(),
                        timeout=3,  # Timeout for response
                    )
                    if isinstance(data, real_time.ReadingError):
                        error = True
                        break
                    if data.value != 0:
                        # Comment out adding to valid_readings list
                        # valid_readings.append(data.value)
                        print(f"Reading: {data.value}")
                        
                        # Instead of publishing to a separate topic, we'll modify the blanket sensor data
                        # Store the latest heart rate value to be added to blanket sensor data
                        self.latest_heart_rate = data.value
                        
                        data_count += 1
                        last_data_time = asyncio.get_event_loop().time()
                except asyncio.TimeoutError:
                    # If timeout occurs, we'll check if we need to resend in the next loop
                    pass  # Just continue checking stopping flag
        finally:
            stopping = True  # Signal keyboard thread to exit
            # Ensure we always send the stop packet
            await self.send_packet(stop_packet)
            logger.info(f"Data collection stopped.")
            
        # Always return None since we're not collecting readings
        return None

    async def get_realtime_reading(self, reading_type: real_time.RealTimeReading) -> None:
        return await self._poll_real_time_reading(reading_type)

    async def set_time(self, ts: datetime) -> None:
        await self.send_packet(set_time.set_time_packet(ts))

    async def blink_twice(self) -> None:
        await self.send_packet(blink_twice.BLINK_TWICE_PACKET)

    async def get_device_info(self) -> dict[str, str]:
        client = self.bleak_client
        data = {}
        device_info_service = client.services.get_service(DEVICE_INFO_UUID)
        assert device_info_service

        hw_info_char = device_info_service.get_characteristic(DEVICE_HW_UUID)
        assert hw_info_char
        hw_version = await client.read_gatt_char(hw_info_char)
        data["hw_version"] = hw_version.decode("utf-8")

        fw_info_char = device_info_service.get_characteristic(DEVICE_FW_UUID)
        assert fw_info_char
        fw_version = await client.read_gatt_char(fw_info_char)
        data["fw_version"] = fw_version.decode("utf-8")

        return data

    async def get_heart_rate_log(self, target: datetime | None = None) -> hr.HeartRateLog | hr.NoData:
        if target is None:
            target = date_utils.start_of_day(date_utils.now())
        await self.send_packet(hr.read_heart_rate_packet(target))
        return await asyncio.wait_for(
            self.queues[hr.CMD_READ_HEART_RATE].get(),
            timeout=2,
        )

    async def get_heart_rate_log_settings(self) -> hr_settings.HeartRateLogSettings:
        await self.send_packet(hr_settings.READ_HEART_RATE_LOG_SETTINGS_PACKET)
        return await asyncio.wait_for(
            self.queues[hr_settings.CMD_HEART_RATE_LOG_SETTINGS].get(),
            timeout=2,
        )

    async def set_heart_rate_log_settings(self, enabled: bool, interval: int) -> None:
        await self.send_packet(hr_settings.hr_log_settings_packet(hr_settings.HeartRateLogSettings(enabled, interval)))

        # clear response from queue as it's unused and wrong
        await asyncio.wait_for(
            self.queues[hr_settings.CMD_HEART_RATE_LOG_SETTINGS].get(),
            timeout=2,
        )

    async def get_steps(self, target: datetime, today: datetime | None = None) -> list[steps.SportDetail] | steps.NoData:
        if today is None:
            today = datetime.now(timezone.utc)

        if target.tzinfo != timezone.utc:
            logger.info("Converting target time to utc")
            target = target.astimezone(tz=timezone.utc)

        days = (today.date() - target.date()).days
        logger.debug(f"Looking back {days} days")

        await self.send_packet(steps.read_steps_packet(days))
        return await asyncio.wait_for(
            self.queues[steps.CMD_GET_STEP_SOMEDAY].get(),
            timeout=2,
        )

    async def reboot(self) -> None:
        await self.send_packet(reboot.REBOOT_PACKET)

    async def raw(self, command: int, subdata: bytearray, replies: int = 0) -> list[bytearray]:
        p = packet.make_packet(command, subdata)
        await self.send_packet(p)

        results = []
        while replies > 0:
            data: bytearray = await asyncio.wait_for(
                self.queues[command].get(),
                timeout=2,
            )
            results.append(data)
            replies -= 1

        return results

    async def get_full_data(self, start: datetime, end: datetime) -> FullData:
        """
        Fetches all data from the ring between start and end. Useful for syncing.
        """
        heart_rate_logs = []
        sport_detail_logs = []
        for d in date_utils.dates_between(start, end):
            heart_rate_logs.append(await self.get_heart_rate_log(d))
            sport_detail_logs.append(await self.get_steps(d))

        return FullData(self.address, heart_rates=heart_rate_logs, sport_details=sport_detail_logs)

# Add a helper function at the end of the file to select a serial port
def select_serial_port():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports available.")
        return None
    
    print("\nAvailable serial ports:")
    for i, port in enumerate(ports):
        print(f"{i+1}. {port.device}: {port.description}")
    
    choice = input("\nSelect a port (number) or press Enter to skip: ")
    if not choice.strip():
        return None
    
    try:
        index = int(choice) - 1
        if 0 <= index < len(ports):
            return ports[index].device
        else:
            print("Invalid selection.")
            return select_serial_port()
    except ValueError:
        print("Please enter a number.")
        return select_serial_port()
