#!/usr/bin/env python3
"""
Example script showing how to use the Colmi R02 client with real-time visualization.

This script:
1. Connects to a Colmi Smart Ring
2. Sets up MQTT communication and subscribes to blanket sensors
3. Sets up serial communication to forward combined sensor data
4. Launches real-time visualization with 6 line graphs showing:
   - Heart rate (from ring)
   - Sound, light, movement, temperature, and humidity (from blanket)
5. Monitors heart rate data and integrates it with blanket sensor data

Usage:
    python colmi_data_visualize.py <device_address>
"""

import asyncio
import sys
import logging
import threading
import time
from pathlib import Path

from colmi_r02_client.client import Client, select_serial_port
from colmi_r02_client.real_time import RealTimeReading

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global flag to control the heartbeat monitor
monitoring_active = False

async def monitor_heartbeat(client):
    """Monitor the heart rate in a separate coroutine."""
    global monitoring_active
    monitoring_active = True
    try:
        logger.info("Starting heart rate monitoring...")
        print("Starting real-time heart rate monitoring.")
        print("Heart rate data will be integrated with blanket sensor data from MQTT.")
        print("Press 'q' to stop monitoring...")
        await client.get_realtime_reading(RealTimeReading.HEART_RATE)
    finally:
        monitoring_active = False
        logger.info("Heart rate monitoring stopped")

async def setup_client(device_address, serial_port, record_file):
    """Set up and initialize the client."""
    logger.info(f"Connecting to device {device_address}...")
    client = Client(
        address=device_address,
        record_to=record_file,
        use_mqtt=True,
        serial_port=serial_port,
        use_visualization=True
    )
    
    await client.connect()
    logger.info("Client connected")
    
    # Get device info
    device_info = await client.get_device_info()
    print(f"Connected to device: {device_info}")
    
    # Get battery info
    battery_info = await client.get_battery()
    print(f"Battery level: {battery_info.level}%")
    
    return client

async def cleanup_client(client):
    """Clean up and disconnect the client."""
    logger.info("Disconnecting client...")
    
    # Disconnect MQTT if enabled
    if client.use_mqtt:
        client.mqtt_client.loop_stop()
        client.mqtt_client.disconnect()
        logger.info("Disconnected from MQTT broker")
    
    # Close serial port if open
    if client.serial_conn and client.serial_conn.is_open:
        client.serial_conn.close()
        logger.info("Closed serial port connection")
    
    await client.disconnect()
    logger.info("Client disconnected")

def main():
    """Main entry point for the application."""
    if len(sys.argv) < 2:
        print("Usage: python colmi_data_visualize.py <device_address>")
        return
    
    device_address = sys.argv[1]
    
    # Prompt the user to select a serial port
    print("Setting up serial connection for forwarding combined sensor data...")
    serial_port = select_serial_port()
    
    # Create the output directory if it doesn't exist
    output_dir = Path("data")
    output_dir.mkdir(exist_ok=True)
    
    # Specify a file to record raw data (optional)
    record_file = output_dir / f"colmi_{device_address.replace(':', '_')}_{time.time()}.bin"
    
    print(f"Connecting to device {device_address}...")
    print(f"Recording raw data to {record_file}")
    print("Setting up MQTT communication with server.nikolaacademy.com")
    if serial_port:
        print(f"Forwarding combined sensor data to serial port {serial_port}")
    print("Starting real-time visualization of all sensor data")
    
    # Set up the event loop
    loop = asyncio.get_event_loop()
    
    # Initialize client
    client = loop.run_until_complete(setup_client(device_address, serial_port, record_file))
    
    try:
        # Start the heart rate monitoring in a separate task
        monitoring_task = loop.create_task(monitor_heartbeat(client))
        
        # Run the visualization in the main thread
        print("Starting visualization window (close the window to exit)")
        client._start_visualization()
        
        # After visualization window is closed, clean up
        if monitoring_active:
            print("Stopping heart rate monitoring...")
            monitoring_task.cancel()
            try:
                loop.run_until_complete(monitoring_task)
            except asyncio.CancelledError:
                pass
        
        # Clean up the client
        loop.run_until_complete(cleanup_client(client))
        
    except KeyboardInterrupt:
        print("Interrupted by user")
        if monitoring_active:
            monitoring_task.cancel()
            try:
                loop.run_until_complete(monitoring_task)
            except asyncio.CancelledError:
                pass
        loop.run_until_complete(cleanup_client(client))
    
    print("Program completed.")

if __name__ == "__main__":
    main() 