# Project Summary: Colmi R02 Smart Ring Client

This document provides a brief overview of each file and function in the `colmi_r02_client` package to help understand the project structure.

## Package Overview

This package provides a Python client for connecting to and communicating with the Colmi R02 Smart Ring via Bluetooth Low Energy (BLE). It supports reading various sensor data (heart rate, steps, battery), setting device time, configuring settings, and syncing data to a SQLite database.

---

## File-by-File Documentation

### `__init__.py`
**Purpose**: Package initialization file that includes the README documentation.

**Contents**:
- Includes the main README.md file in the package documentation

---

### `battery.py`
**Purpose**: Handles battery level and charging status queries.

**Functions**:
- `parse_battery(packet: bytearray) -> BatteryInfo`: Parses battery information from a response packet
  - Extracts battery level (0-100) from packet[1]
  - Extracts charging status (boolean) from packet[2]

**Constants**:
- `CMD_BATTERY = 3`: Command code for battery queries
- `BATTERY_PACKET`: Pre-built packet for requesting battery info

**Data Classes**:
- `BatteryInfo`: Dataclass containing `battery_level` (int) and `charging` (bool)

---

### `blink_twice.py`
**Purpose**: Simple command module to make the ring blink twice (visual indicator).

**Constants**:
- `CMD_BLINK_TWICE = 16`: Command code for blink twice
- `BLINK_TWICE_PACKET`: Pre-built packet to trigger the blink

---

### `cli.py`
**Purpose**: Command-line interface using asyncclick for interacting with the ring.

**Main Functions**:
- `cli_client()`: Main CLI group with options for:
  - `--debug/--no-debug`: Enable debug logging
  - `--record/--no-record`: Record all packets to file
  - `--address`: Bluetooth address (preferred)
  - `--name`: Bluetooth device name (slower, works on macOS)
- `info()`: Get device info and battery level
- `get_heart_rate_log()`: Get heart rate data for a specific date
- `set_time()`: Set the ring's internal clock
- `get_heart_rate_log_settings()`: Get current heart rate logging settings
- `set_heart_rate_log_settings()`: Configure heart rate logging (enable/disable, interval)
- `get_real_time()`: Get real-time measurements (heart rate, SPO2, etc.)
- `get_steps()`: Get step data for a date (with optional CSV output)
- `reboot()`: Reboot the ring
- `raw()`: Send raw command to the ring
- `sync()`: Sync all data from ring to SQLite database
- `scan()`: Scan for available Colmi devices

**Constants**:
- `DEVICE_NAME_PREFIXES`: List of known device name prefixes for scanning

---

### `client.py`
**Purpose**: Core client class for BLE communication with the ring. Handles connection, packet sending/receiving, and integrates with MQTT, serial port communication, and visualization.

**Main Class: `Client`**

**Initialization Parameters**:
- `address`: Bluetooth address of the ring
- `record_to`: Optional path to record packets
- `use_mqtt`: Enable MQTT publishing (default: True). If connection fails, MQTT is automatically disabled and data collection continues
- `serial_port`: Serial port name (e.g., "COM4" or "/dev/ttyUSB0"). If None, will auto-detect and prompt user to select
- `baud_rate`: Serial port baud rate (default: 115200)
- `use_visualization`: Enable real-time visualization (default: True)

**Key Methods**:
- `__aenter__()` / `__aexit__()`: Async context manager for connection lifecycle
- `connect()`: Establish BLE connection and set up notification handlers
- `disconnect()`: Close BLE connection
- `send_packet(packet: bytearray)`: Send a packet to the ring
- `get_battery() -> BatteryInfo`: Get battery level and charging status
- `get_device_info() -> dict`: Get hardware and firmware version
- `set_time(ts: datetime)`: Set ring's internal clock
- `blink_twice()`: Trigger ring to blink twice
- `get_heart_rate_log(target: datetime) -> HeartRateLog | NoData`: Get heart rate data for a date
- `get_heart_rate_log_settings() -> HeartRateLogSettings`: Get HR logging configuration
- `set_heart_rate_log_settings(enabled: bool, interval: int)`: Configure HR logging
- `get_steps(target: datetime) -> list[SportDetail] | NoData`: Get step data for a date
- `get_realtime_reading(reading_type: RealTimeReading)`: Start real-time sensor reading
- `_poll_real_time_reading(reading_type: RealTimeReading)`: Poll for real-time readings
- `reboot()`: Reboot the ring
- `raw(command: int, subdata: bytearray, replies: int) -> list[bytearray]`: Send raw command
- `get_full_data(start: datetime, end: datetime) -> FullData`: Fetch all data in date range

**Internal Methods**:
- `_handle_tx()`: Callback for incoming BLE packets
- `_on_mqtt_connect()`: MQTT connection callback
- `_on_mqtt_message()`: MQTT message handler
- `_log_serial_data_to_csv()`: Log serial/sensor data to CSV file
- `_log_heartrate_to_csv()`: Log heart rate data to CSV file
- `_start_visualization()`: Start visualization in main thread
- `start_visualization_nonblocking()`: Start visualization in background thread

**Error Handling**:
- MQTT connection failures are handled gracefully - if connection fails (e.g., no WiFi), the client automatically disables MQTT and continues with data collection
- Serial port connection failures are handled with try-except blocks, allowing the client to continue without serial port if unavailable

**Constants**:
- `UART_SERVICE_UUID`, `UART_RX_CHAR_UUID`, `UART_TX_CHAR_UUID`: BLE service/characteristic UUIDs
- `DEVICE_INFO_UUID`, `DEVICE_HW_UUID`, `DEVICE_FW_UUID`: Device info service UUIDs
- `COMMAND_HANDLERS`: Dictionary mapping command codes to parser functions

**Data Classes**:
- `FullData`: Container for synced data (address, heart_rates, sport_details)

**Helper Functions**:
- `select_serial_port() -> Optional[str]`: Automatically detect available serial ports and prompt user to select one

---

### `date_utils.py`
**Purpose**: Utility functions for datetime manipulation, especially for handling ring data timestamps.

**Functions**:
- `start_of_day(ts: datetime) -> datetime`: Get midnight of the given day
- `end_of_day(ts: datetime) -> datetime`: Get last moment of the given day
- `dates_between(start: datetime, end: datetime) -> Iterator[datetime]`: Generator for all days between start and end
- `now() -> datetime`: Get current UTC datetime
- `minutes_so_far(dt: datetime) -> int`: Calculate minutes elapsed in the day (with +1 offset)
- `is_today(ts: datetime) -> bool`: Check if datetime is today
- `naive_to_aware(dt: datetime) -> datetime`: Convert naive datetime to UTC-aware

---

### `db.py`
**Purpose**: SQLite database models and operations for storing ring data.

**Database Models**:
- `Base`: SQLAlchemy declarative base
- `DateTimeInUTC`: Custom type decorator ensuring all datetimes are UTC-aware
- `Ring`: Table for ring devices (address, relationships to heart_rates, sport_details, syncs)
- `Sync`: Table tracking sync operations (timestamp, comment, relationships)
- `HeartRate`: Table for heart rate readings (reading, timestamp, ring_id, sync_id)
- `SportDetail`: Table for step/activity data (calories, steps, distance, timestamp, ring_id, sync_id)

**Functions**:
- `get_db_session(path: Path | None) -> Session`: Create database session and tables
- `create_or_find_ring(session: Session, address: str) -> Ring`: Get or create ring record
- `full_sync(session: Session, data: FullData) -> None`: Sync all data to database
- `_add_heart_rate()`: Internal function to add heart rate data to database
- `_add_sport_details()`: Internal function to add step data to database
- `get_last_sync(session: Session, ring_address: str) -> datetime | None`: Get last sync timestamp

**Event Handlers**:
- `set_sqlite_pragma()`: Enable foreign key checks in SQLite

---

### `hr_settings.py`
**Purpose**: Heart rate logging settings (enable/disable periodic HR measurement and interval configuration).

**Functions**:
- `parse_heart_rate_log_settings(packet: bytearray) -> HeartRateLogSettings`: Parse settings from response packet
- `hr_log_settings_packet(settings: HeartRateLogSettings) -> bytearray`: Create packet to set settings

**Constants**:
- `CMD_HEART_RATE_LOG_SETTINGS = 22`: Command code for HR settings
- `READ_HEART_RATE_LOG_SETTINGS_PACKET`: Pre-built packet to query settings

**Data Classes**:
- `HeartRateLogSettings`: Dataclass with `enabled` (bool) and `interval` (int, in minutes)

---

### `hr.py`
**Purpose**: Heart rate log reading and parsing. Handles multi-packet responses for daily heart rate data.

**Functions**:
- `read_heart_rate_packet(target: datetime) -> bytearray`: Create packet to request HR data for a date
- `_add_times(heart_rates: list[int], ts: datetime) -> list[tuple[int, datetime]]`: Add timestamps to HR readings (5-minute intervals)

**Classes**:
- `HeartRateLog`: Dataclass containing:
  - `heart_rates`: List of 288 readings (5-minute intervals for 24 hours)
  - `timestamp`: Date of the log
  - `size`, `index`, `range`: Metadata about the data
  - `heart_rates_with_times()`: Method returning list of (reading, timestamp) tuples
- `NoData`: Marker class returned when no data is available
- `HeartRateLogParser`: Stateful parser for multi-packet heart rate responses
  - `reset()`: Reset parser state
  - `is_today() -> bool`: Check if current log is for today
  - `parse(packet: bytearray) -> HeartRateLog | NoData | None`: Parse packet (returns None for intermediate packets)
  - `heart_rates` (property): Normalized and cleaned heart rate list

**Constants**:
- `CMD_READ_HEART_RATE = 21`: Command code for reading heart rate logs

---

### `packet.py`
**Purpose**: Low-level packet construction and checksum calculation for ring communication.

**Functions**:
- `make_packet(command: int, sub_data: bytearray | None = None) -> bytearray`: Create a 16-byte packet
  - Validates command (0-255) and sub_data length (0-14 bytes)
  - Calculates and appends checksum
- `checksum(packet: bytearray) -> int`: Calculate packet checksum (sum of all bytes mod 255)

**Note**: All packets are exactly 16 bytes with the last byte being the checksum.

---

### `pretty_print.py`
**Purpose**: Utility functions for formatting and displaying structured data.

**Functions**:
- `print_lists(rows: list[list[Any]], header: bool = False) -> str`: Format list of lists as aligned table
- `print_dicts(rows: list[dict]) -> str`: Format list of dictionaries as table with headers
- `print_dataclasses(dcs: list[Any]) -> str`: Format list of dataclasses as table

---

### `real_time.py`
**Purpose**: Real-time sensor reading (heart rate, SPO2, blood pressure, etc.) via streaming.

**Functions**:
- `get_start_packet(reading_type: RealTimeReading) -> bytearray`: Create packet to start real-time reading
- `get_continue_packet(reading_type: RealTimeReading) -> bytearray`: Create packet to continue reading
- `get_stop_packet(reading_type: RealTimeReading) -> bytearray`: Create packet to stop reading
- `parse_real_time_reading(packet: bytearray) -> Reading | ReadingError`: Parse real-time reading response

**Enums**:
- `Action`: START, PAUSE, CONTINUE, STOP
- `RealTimeReading`: HEART_RATE, BLOOD_PRESSURE, SPO2, FATIGUE, HEALTH_CHECK, ECG, PRESSURE, BLOOD_SUGAR, HRV

**Constants**:
- `CMD_START_REAL_TIME = 105`: Command to start real-time reading
- `CMD_STOP_REAL_TIME = 106`: Command to stop real-time reading
- `CMD_REAL_TIME_HEART_RATE = 30`: Legacy command (unused)
- `REAL_TIME_MAPPING`: Dictionary mapping string names to RealTimeReading enum values

**Data Classes**:
- `Reading`: Contains `kind` (RealTimeReading) and `value` (int)
- `ReadingError`: Contains `kind` (RealTimeReading) and `code` (int)

---

### `reboot.py`
**Purpose**: Simple command module to reboot the ring.

**Constants**:
- `CMD_REBOOT = 8`: Command code for reboot
- `REBOOT_PACKET`: Pre-built packet to trigger reboot

---

### `set_time.py`
**Purpose**: Set the ring's internal clock (required for accurate data timestamps).

**Functions**:
- `set_time_packet(target: datetime) -> bytearray`: Create packet to set ring time
  - Converts datetime to UTC
  - Encodes date/time in BCD format
  - Sets language to English (1) or Chinese (0)
- `byte_to_bcd(b: int) -> int`: Convert integer (0-99) to Binary Coded Decimal
- `parse_set_time_packet(packet: bytearray) -> dict[str, bool | int]`: Parse capability response (mostly unused)

**Constants**:
- `CMD_SET_TIME = 1`: Command code for setting time

**Note**: The ring uses its internal clock to timestamp all logged data, so setting the time is critical.

---

### `steps.py`
**Purpose**: Step data reading and parsing. Handles daily activity data (steps, calories, distance).

**Functions**:
- `read_steps_packet(day_offset: int = 0) -> bytearray`: Create packet to request step data
  - `day_offset`: Days ago (0 = today, 1 = yesterday, etc.)
- `bcd_to_decimal(b: int) -> int`: Convert Binary Coded Decimal to integer

**Classes**:
- `SportDetail`: Dataclass containing:
  - `year`, `month`, `day`, `time_index`: Date and 15-minute interval index
  - `calories`, `steps`, `distance`: Activity metrics
  - `timestamp` (property): Computed datetime from date components
- `NoData`: Marker class returned when no data is available
- `SportDetailParser`: Stateful parser for multi-packet step data responses
  - `reset()`: Reset parser state
  - `parse(packet: bytearray) -> list[SportDetail] | None | NoData`: Parse packet (returns None for intermediate packets)

**Constants**:
- `CMD_GET_STEP_SOMEDAY = 67`: Command code for reading step data

**Note**: Data is organized in 15-minute intervals throughout the day.

---

### `visualization.py`
**Purpose**: Real-time visualization of sensor data using matplotlib.

**Main Class: `SensorDataVisualizer`**

**Initialization Parameters**:
- `max_points: int = 100`: Maximum number of data points to display in history

**Methods**:
- `__init__(max_points: int)`: Initialize visualizer with data storage deques
- `setup_plot()`: Create matplotlib figure with 3x2 subplot grid (6 sensors)
- `update_data(new_data: Dict[str, Any])`: Add new sensor data to visualization
- `start()`: Start the visualization window (blocks until closed)
- `stop()`: Stop visualization and close window
- `_update_plot(frame)`: Animation callback to update plots
- `_get_unit(data_type: str) -> str`: Get unit label for each sensor type
- `_get_y_limits(data_type: str) -> tuple`: Get default y-axis limits for each sensor

**Supported Sensors**:
- `heartrate`: Heart rate in BPM (red)
- `sound`: Sound intensity in dB (blue)
- `light`: Light level in Lux (orange)
- `movement`: Movement/acceleration in g (green)
- `temperature`: Temperature in °C (purple)
- `humidity`: Humidity in % (teal)

**Helper Functions**:
- `parse_sensor_data(data_str: str) -> Optional[Dict[str, Any]]`: Parse JSON string to sensor data dictionary

**Note**: Uses dark theme and updates plots every 100ms. Thread-safe with locking for concurrent updates.

---

## Data Flow Overview

1. **Connection**: `Client` connects via BLE using `bleak` library
2. **Command Sending**: Commands are formatted as 16-byte packets via `packet.make_packet()`
3. **Response Handling**: Incoming packets are routed via `COMMAND_HANDLERS` to appropriate parsers
4. **Data Parsing**: Multi-packet responses use stateful parsers (e.g., `HeartRateLogParser`, `SportDetailParser`)
5. **Data Storage**: Synced data can be stored in SQLite via `db.full_sync()`
6. **Real-time**: Real-time readings can be streamed and visualized or published via MQTT

---

## Key Dependencies

- `bleak`: BLE communication
- `asyncclick`: CLI framework
- `sqlalchemy`: Database ORM
- `matplotlib`: Visualization
- `paho-mqtt`: MQTT client
- `pyserial`: Serial port communication

---

## Common Patterns

1. **Packet Structure**: All packets are 16 bytes with command in byte[0] and checksum in byte[15]
2. **Multi-packet Responses**: Some commands (HR logs, steps) return multiple packets parsed by stateful parsers
3. **UTC Timezone**: All timestamps are stored and handled in UTC
4. **BCD Encoding**: Dates/times in packets use Binary Coded Decimal format
5. **Error Handling**: Commands return `NoData` classes when no data is available
6. **Async/Await**: All BLE operations are asynchronous

---

## Notes for Future Development

- The ring's internal clock must be set before reading logged data
- Heart rate logs contain 288 readings (5-minute intervals for 24 hours)
- Step data uses 15-minute intervals (96 intervals per day)
- Some packet fields are not fully understood (marked with comments)
- MQTT integration allows publishing sensor data to external systems, but is optional and gracefully handles connection failures
- **Offline Operation**: The client works completely offline - all core data collection (BLE communication, serial port, CSV logging, SQLite storage) functions without WiFi or network connectivity
- Visualization supports 6 sensor types with automatic scaling
- Serial port communication uses pyserial with automatic port detection
- If `serial_port` is None during Client initialization, the system will automatically detect and prompt for port selection
- CSV logging automatically creates timestamped files in the `data/` directory for both serial sensor data and heart rate data