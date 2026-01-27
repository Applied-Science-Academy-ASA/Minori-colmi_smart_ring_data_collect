import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
import logging
from pathlib import Path
from types import TracebackType
from typing import Any, Optional
import json
import os
import sys
import threading
import time
import csv

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
import paho.mqtt.client as mqtt
import serial  # pyserial library
import serial.tools.list_ports  # pyserial library

from colmi_r02_client import battery, date_utils, steps, set_time, blink_twice, hr, hr_settings, packet, reboot, real_time
# from colmi_r02_client.visualization import SensorDataVisualizer

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


def select_serial_port() -> Optional[str]:
    """
    Automatically detect available serial ports and prompt user to select one.
    
    Returns:
        Selected port name (e.g., "COM4" or "/dev/ttyUSB0") or None if no port selected
    """
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No serial ports available.")
        return None
    
    print("\nAvailable serial ports:")
    print(f"{'#':<4} {'Port':<15} {'Description'}")
    print("-" * 60)
    for i, port in enumerate(ports, 1):
        description = port.description if port.description else "N/A"
        print(f"{i:<4} {port.device:<15} {description}")
    
    while True:
        choice = input("\nSelect a port (number) or press Enter to skip: ").strip()
        if not choice:
            return None
        
        try:
            index = int(choice) - 1
            if 0 <= index < len(ports):
                selected_port = ports[index].device
                print(f"Selected port: {selected_port}")
                return selected_port
            else:
                print(f"Invalid selection. Please enter a number between 1 and {len(ports)}.")
        except ValueError:
            print("Please enter a valid number.")


class Client:
    def __init__(self, address: str, record_to: Path | None = None, use_mqtt: bool = True, 
                 serial_port: Optional[str] = None, baud_rate: int = 115200,
                 use_visualization: bool = False):  # Disabled by default
        self.address = address
        self.bleak_client = BleakClient(self.address)
        self.queues: dict[int, asyncio.Queue] = {cmd: asyncio.Queue() for cmd in COMMAND_HANDLERS}
        self.record_to = record_to
        
        # Track the latest heart rate reading
        self.latest_heart_rate = None
        
        # Serial port setup
        self.serial_conn = None
        if serial_port is None:
            # Auto-detect and prompt user to select port
            serial_port = select_serial_port()
        
        self.serial_port = serial_port
        if serial_port:
            try:
                self.serial_conn = serial.Serial(serial_port, baud_rate, timeout=1)
                logger.info(f"Connected to serial port {serial_port} at {baud_rate} baud")
                print(f"Connected to serial port {serial_port} at {baud_rate} baud")
            except serial.SerialException as e:
                logger.error(f"Failed to connect to serial port {serial_port}: {e}")
                print(f"Failed to connect to serial port {serial_port}: {e}")
                self.serial_conn = None
        
        # Visualization setup - COMMENTED OUT
        # self.use_visualization = use_visualization
        # self.visualizer = None
        # if use_visualization:
        #     # Create the visualizer but don't start it yet
        #     self.visualizer = SensorDataVisualizer()
        #     print("Created visualization instance")
        #     logger.info("Created visualization instance")
        # else:
        #     print("Visualization disabled")
        #     logger.info("Visualization disabled")
        self.use_visualization = False
        self.visualizer = None
        print("Visualization disabled")
        logger.info("Visualization disabled")

        # CSV logging setup
        self.data_dir = Path("data")
        self.data_dir.mkdir(exist_ok=True)
        
        # Generate timestamped filenames
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.serial_csv_path = self.data_dir / f"serial_data_{timestamp}.csv"
        self.heartrate_csv_path = self.data_dir / f"heartrate_data_{timestamp}.csv"
        
        # Initialize CSV files with headers
        self.csv_lock = threading.Lock()
        
        # Initialize serial data CSV
        with open(self.serial_csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'sound', 'light', 'movement', 'temperature', 'humidity', 'heartrate'])
        
        # Initialize heart rate CSV
        with open(self.heartrate_csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['timestamp', 'heartrate'])
        
        logger.info(f"CSV logging initialized: {self.serial_csv_path}, {self.heartrate_csv_path}")
        print(f"CSV logging initialized:")
        print(f"  Serial data: {self.serial_csv_path}")
        print(f"  Heart rate: {self.heartrate_csv_path}")

        # MQTT setup
        self.use_mqtt = use_mqtt
        if use_mqtt:
            self.mqtt_client = mqtt.Client()
            
            # Define MQTT callbacks
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            
            # Connect to the broker
            self.mqtt_client.connect("broker.mqtt.cool", 1883, 60)
            self.mqtt_client.loop_start()
            logger.info("Connected to MQTT broker")

    def _log_serial_data_to_csv(self, data: dict):
        """Log serial/sensor data to CSV file."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.csv_lock:
            try:
                with open(self.serial_csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        timestamp,
                        data.get('sound', ''),
                        data.get('light', ''),
                        data.get('movement', ''),
                        data.get('temperature', ''),
                        data.get('humidity', ''),
                        data.get('heartrate', '')
                    ])
                    f.flush()  # Explicitly flush buffer to ensure data is written to disk
                    os.fsync(f.fileno())  # Force write to disk (important for Linux/Raspberry Pi)
            except Exception as e:
                logger.error(f"Error writing serial data to CSV: {e}")
    
    def _log_heartrate_to_csv(self, heartrate: int):
        """Log heart rate data to CSV file."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.csv_lock:
            try:
                with open(self.heartrate_csv_path, 'a', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow([timestamp, heartrate])
                    f.flush()  # Explicitly flush buffer to ensure data is written to disk
                    os.fsync(f.fileno())  # Force write to disk (important for Linux/Raspberry Pi)
            except Exception as e:
                logger.error(f"Error writing heart rate to CSV: {e}")

    # Visualization methods - COMMENTED OUT
    # def _start_visualization(self):
    #     """Start the visualization in the main thread."""
    #     if self.visualizer:
    #         logger.info("Starting visualization in main thread")
    #         self.visualizer.start()
    #         logger.info("Visualization window closed")
    #         
    # def start_visualization_nonblocking(self):
    #     """
    #     Start visualization in a separate thread.
    #     Note: This might cause issues as matplotlib prefers to run in the main thread.
    #     """
    #     if self.visualizer and not self.visualizer.running:
    #         logger.info("Starting visualization in background thread")
    #         print("Starting visualization in background thread")
    #         self.viz_thread = threading.Thread(target=self._start_visualization)
    #         self.viz_thread.daemon = True
    #         self.viz_thread.start()
    #         logger.info("Visualization thread started")

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
                # Visualization - COMMENTED OUT
                # self.start_visualization_nonblocking()
                try:
                    # Parse the JSON
                    data = json.loads(payload)
                    
                    # Add heart rate to the data if available
                    if self.latest_heart_rate is not None:
                        data["heartrate"] = self.latest_heart_rate
                        # Convert back to JSON for logging
                        enhanced_payload = json.dumps(data)
                        logger.info(f"Added heart rate to blanket sensor data: {enhanced_payload}")
                        print(f"Added heart rate to blanket sensor data: {enhanced_payload}")
                    else:
                        logger.info("No heart rate data available yet")
                    
                    # Update visualization regardless of heart rate availability - COMMENTED OUT
                    # if self.visualizer:
                    #     # Ensure all values are properly converted to float
                    #     cleaned_data = {}
                    #     for key, value in data.items():
                    #         try:
                    #             cleaned_data[key] = float(value)
                    #         except (ValueError, TypeError):
                    #             logger.warning(f"Could not convert {key}={value} to float")
                    #             # Skip this value
                    #     
                    #     logger.info(f"Updating visualization with data: {cleaned_data}")
                    #     self.visualizer.update_data(cleaned_data)
                    #     print(f"Updated visualization with data points: {list(cleaned_data.keys())}")
                    
                    # Format display based on the topic
                    print("\n===== SENSOR DATA =====")
                    for key, value in data.items():
                        print(f"  {key}: {value}")
                    print("=======================\n")
                    
                    # Log serial data to CSV
                    self._log_serial_data_to_csv(data)
                    
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
        
        # Disconnect MQTT if enabled
        if self.use_mqtt:
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
            logger.info("Disconnected from MQTT broker")
        
        # Stop visualization if enabled - COMMENTED OUT
        # if self.visualizer:
        #     self.visualizer.stop()
        #     logger.info("Stopped visualization")
        
        # Close serial port if open
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            logger.info("Closed serial port connection")
            
        await self.disconnect()

    async def connect(self):
        """Connect to the BLE device with retry logic for timeout errors and device not found errors."""
        retry_delay = 2  # Wait 2 seconds between retries
        attempt = 0
        
        while True:
            attempt += 1
            try:
                await self.bleak_client.connect()
                logger.info(f"Successfully connected to {self.address} after {attempt} attempt(s)")
                print(f"Successfully connected to {self.address} after {attempt} attempt(s)")
                break
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                logger.warning(f"Connection timeout (attempt {attempt}), retrying in {retry_delay} seconds...")
                print(f"Connection timeout, retrying in {retry_delay} seconds... (attempt {attempt})")
                await asyncio.sleep(retry_delay)
                # Recreate the client if it was cancelled
                if isinstance(e, asyncio.CancelledError):
                    self.bleak_client = BleakClient(self.address)
            except BleakError as e:
                # Retry on BleakError (e.g., device not found)
                logger.warning(f"Connection error (attempt {attempt}): {e}, retrying in {retry_delay} seconds...")
                print(f"Connection error, retrying in {retry_delay} seconds... (attempt {attempt})")
                await asyncio.sleep(retry_delay)
                # Recreate the client to reset the connection state
                self.bleak_client = BleakClient(self.address)
            except Exception as e:
                # For other exceptions, log and re-raise
                logger.error(f"Connection error: {e}")
                raise

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
                        # Ignore errors and continue polling instead of breaking
                        logger.debug(f"Reading error (code {data.code}), continuing...")
                        continue
                    if data.value != 0:
                        # Comment out adding to valid_readings list
                        # valid_readings.append(data.value)
                        print(f"Reading: {data.value}")
                        
                        # Instead of publishing to a separate topic, we'll modify the blanket sensor data
                        # Store the latest heart rate value to be added to blanket sensor data
                        self.latest_heart_rate = data.value
                        
                        # Log heart rate to CSV
                        self._log_heartrate_to_csv(data.value)
                        
                        data_count += 1
                        last_data_time = asyncio.get_event_loop().time()
                    else:
                        # No heart rate detected (value == 0), but continue polling
                        logger.debug("No heart rate detected (value == 0), continuing...")
                        continue
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
