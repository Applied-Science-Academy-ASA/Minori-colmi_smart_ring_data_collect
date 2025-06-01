import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
import logging
from pathlib import Path
from types import TracebackType
from typing import Any
import time

from bleak import BleakClient
from bleak.backends.characteristic import BleakGATTCharacteristic

from colmi_r02_client import battery, date_utils, steps, set_time, blink_twice, hr, hr_settings, packet, reboot, real_time

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
    def __init__(self, address: str, record_to: Path | None = None):
        self.address = address
        self.bleak_client = BleakClient(self.address)
        self.queues: dict[int, asyncio.Queue] = {cmd: asyncio.Queue() for cmd in COMMAND_HANDLERS}
        self.record_to = record_to

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
        await self.bleak_client.write_gatt_char(self.rx_char, packet, response=False)

    async def get_battery(self) -> battery.BatteryInfo:
        await self.send_packet(battery.BATTERY_PACKET)
        result = await self.queues[battery.CMD_BATTERY].get()
        assert isinstance(result, battery.BatteryInfo)
        return result

    async def _poll_real_time_reading(self, reading_type: real_time.RealTimeReading) -> list[int] | None:
        """
        Original real-time polling method - commented out
        """
        # import asyncio
        # import sys
        # import threading
        # print(reading_type)
        # start_packet = real_time.get_start_packet(reading_type)
        # stop_packet = real_time.get_stop_packet(reading_type)
        # 
        # await self.send_packet(start_packet)
        # 
        # valid_readings: list[int] = []
        # error = False
        # stopping = False
        # 
        # # Use a separate thread for keyboard monitoring to not block asyncio
        # def check_for_quit():
        #     nonlocal stopping
        #     print("Press 'q' to stop collecting readings...")
        #     while not stopping:
        #         if sys.platform == 'win32':
        #             import msvcrt
        #             if msvcrt.kbhit() and msvcrt.getch() == b'q':
        #                 print("Stopping data collection...")
        #                 stopping = True
        #                 break
        #         else:
        #             # For Unix-like systems
        #             import select
        #             rlist, _, _ = select.select([sys.stdin], [], [], 0.5)
        #             if rlist and sys.stdin.read(1) == 'q':
        #                 print("Stopping data collection...")
        #                 stopping = True
        #                 break
        #             
        # # Start keyboard monitoring in separate thread
        # keyboard_thread = threading.Thread(target=check_for_quit)
        # keyboard_thread.daemon = True  # Thread will exit when main program exits
        # keyboard_thread.start()
        # 
        # try:
        #     data_count = 0
        #     last_data_time = asyncio.get_event_loop().time()
        #     
        #     while not stopping:
        #         try:
        #             # Resend start packet every 10 readings or if no data for 5 seconds
        #             current_time = asyncio.get_event_loop().time()
        #             print(f"Current time: {current_time}, last data time: {last_data_time}, data count: {data_count}")
        #             if data_count >= 10 or (data_count >= 10 and (current_time - last_data_time > 5)):
        #                 logger.debug(f"Sending packet: {start_packet}")
        #                 print(f"Sending packet: {start_packet}")
        #                 await self.send_packet(start_packet)
        #                 data_count = 0
        #                 last_data_time = current_time
        #             
        #             data: real_time.Reading | real_time.ReadingError = await asyncio.wait_for(
        #                 self.queues[real_time.CMD_START_REAL_TIME].get(),
        #                 timeout=3,  # Timeout for response
        #             )
        #             if isinstance(data, real_time.ReadingError):
        #                 error = True
        #                 break
        #             if data.value != 0:
        #                 valid_readings.append(data.value)
        #                 print(f"Reading: {data.value}")
        #                 data_count += 1
        #                 last_data_time = asyncio.get_event_loop().time()
        #         except asyncio.TimeoutError:
        #             # If timeout occurs, we'll check if we need to resend in the next loop
        #             pass  # Just continue checking stopping flag
        # finally:
        #     stopping = True  # Signal keyboard thread to exit
        #     # Ensure we always send the stop packet
        #     await self.send_packet(stop_packet)
        #     logger.info(f"Data collection stopped. Collected {len(valid_readings)} readings.")
        #     
        # if error:
        #     return None
        # return valid_readings

        # New implementation using Arduino IoT Client
        import asyncio
        import sys
        import threading
        import time
        import os
        
        # First, send the start packet to begin data collection
        print(f"Starting real-time reading for {reading_type}")
        start_packet = real_time.get_start_packet(reading_type)
        stop_packet = real_time.get_stop_packet(reading_type)
        
        await self.send_packet(start_packet)
        
        # We'll still collect readings locally for return value
        valid_readings: list[int] = []
        error = False
        stopping = False
        
        # To use Arduino IoT Client, we need to set up authentication
        try:
            # Try importing all required packages
            try:
                # Arduino IoT Client
                import arduino_iot_client
                from arduino_iot_client.configuration import Configuration
                from arduino_iot_client.api_client import ApiClient
                from arduino_iot_client.apis.properties_v2_api import PropertiesV2Api
                from arduino_iot_client.apis.things_v2_api import ThingsV2Api
                
                # OAuth packages
                from oauthlib.oauth2 import BackendApplicationClient
                from requests_oauthlib import OAuth2Session
            except ImportError as e:
                logger.error(f"Missing required package: {e}")
                logger.error("Please install required packages with: pip install arduino-iot-client requests-oauthlib")
                error = True
                return None
                
            # Setup for stopping via keyboard
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
            keyboard_thread.daemon = True
            keyboard_thread.start()
            
            # You'll need to set these with your actual Arduino IoT credentials
            # Ideally these should come from environment variables or a secure config
            client_id = os.environ.get("ARDUINO_CLIENT_ID_SLEEPMONITOR_MINORI")
            client_secret = os.environ.get("ARDUINO_CLIENT_SECRET_SLEEPMONITOR_MINORI")
            thing_id = os.environ.get("ARDUINO_THING_ID_SLEEPMONITOR_MINORI")
            property_id = os.environ.get("ARDUINO_PROPERTY_ID_SLEEPMONITOR_MINORI")
            
            if not all([client_id, client_secret, thing_id, property_id]):
                logger.error("Arduino IoT credentials not provided. Please set environment variables: "
                            "ARDUINO_CLIENT_ID_SLEEPMONITOR_MINORI, ARDUINO_CLIENT_SECRET_SLEEPMONITOR_MINORI, ARDUINO_THING_ID_SLEEPMONITOR_MINORI, ARDUINO_PROPERTY_ID_SLEEPMONITOR_MINORI")
                error = True
                return None
                
            # Configure Arduino IoT Client with proper OAuth2 authentication
            from oauthlib.oauth2 import BackendApplicationClient
            from requests_oauthlib import OAuth2Session
            
            # Set up OAuth2 session
            auth_url = "https://api2.arduino.cc/iot/v1/clients/token"
            oauth_client = BackendApplicationClient(client_id=client_id)
            oauth = OAuth2Session(client=oauth_client)
            
            # Get token
            logger.info("Authenticating with Arduino IoT Cloud...")
            token = oauth.fetch_token(
                token_url=auth_url,
                client_id=client_id,
                client_secret=client_secret,
                include_client_id=True,
                audience="https://api2.arduino.cc/iot"
            )
            
            logger.info("Successfully authenticated with Arduino IoT Cloud")
            
            # Use the token for API requests
            configuration = Configuration(host="https://api2.arduino.cc/iot")
            configuration.access_token = token['access_token']
            
            # Create API client
            api_client = ApiClient(configuration)
            
            # Initialize APIs
            things_api = ThingsV2Api(api_client)
            properties_api = PropertiesV2Api(api_client)
            
            try:
                # Set up variables for token refresh
                token_expiry_time = time.time() + token.get('expires_in', 3600) - 300  # Refresh 5 min before expiry
                
                # Main data collection loop
                while not stopping:
                    # Check if token needs refreshing
                    current_time = time.time()
                    if current_time > token_expiry_time:
                        logger.info("Refreshing Arduino IoT Cloud access token...")
                        try:
                            token = oauth.fetch_token(
                                token_url=auth_url,
                                client_id=client_id,
                                client_secret=client_secret,
                                include_client_id=True,
                                audience="https://api2.arduino.cc/iot"
                            )
                            configuration.access_token = token['access_token']
                            token_expiry_time = time.time() + token.get('expires_in', 3600) - 300
                            logger.info("Token refreshed successfully")
                        except Exception as e:
                            logger.error(f"Failed to refresh token: {e}")
                            
                    try:
                        # Get data from the device
                        data: real_time.Reading | real_time.ReadingError = await asyncio.wait_for(
                            self.queues[real_time.CMD_START_REAL_TIME].get(),
                            timeout=3,
                        )
                        
                        if isinstance(data, real_time.ReadingError):
                            error = True
                            break
                            
                        if data.value != 0:
                            # Store the reading locally
                            valid_readings.append(data.value)
                            print(f"Reading: {data.value}")
                            
                            # Send data to Arduino IoT Cloud
                            try:
                                # Format data correctly for Arduino IoT Cloud
                                # For numeric properties, we just need the value
                                properties_api.properties_v2_publish(thing_id, property_id, data.value)
                                logger.info(f"Sent value {data.value} to Arduino IoT Cloud")
                            except Exception as e:
                                logger.error(f"Error sending data to Arduino IoT Cloud: {e}")
                                logger.exception(e)  # Log full exception details
                    
                    except asyncio.TimeoutError:
                        # If we time out waiting for data, check if we're stopping
                        pass
                        
            except Exception as e:
                logger.error(f"Error in Arduino IoT data collection: {e}")
                error = True
                
        except ImportError:
            logger.error("Arduino IoT Client not installed. Run 'pip install arduino-iot-client'")
            error = True
        
        finally:
            # Always stop data collection and cleanup
            stopping = True
            await self.send_packet(stop_packet)
            logger.info(f"Data collection stopped. Collected {len(valid_readings)} readings.")
        
        if error:
            return None
        return valid_readings

    async def get_realtime_reading(self, reading_type: real_time.RealTimeReading) -> list[int] | None:
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
