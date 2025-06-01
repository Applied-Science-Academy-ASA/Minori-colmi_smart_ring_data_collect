#!/usr/bin/env python3
"""
Test script for the sensor data visualization.
This will create sample data and update the visualization every second.
"""

import time
import threading
import random
import logging
import json
from datetime import datetime
import queue

from colmi_r02_client.visualization import SensorDataVisualizer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Create a queue for thread-safe data passing
data_queue = queue.Queue()

def generate_sample_data():
    """Generate realistic sample sensor data."""
    return {
        "heartrate": random.randint(60, 100),
        "sound": random.uniform(30, 70),
        "light": random.uniform(100, 500),
        "movement": random.uniform(0, 1.5),
        "temperature": random.uniform(20, 28),
        "humidity": random.uniform(30, 60)
    }

def data_generator_thread():
    """Generate sample data in a background thread."""
    count = 0
    try:
        # Generate 100 data points (one per second)
        while count < 100:
            data = generate_sample_data()
            logger.info(f"Generated data point #{count+1}: {data}")
            data_queue.put(data)
            count += 1
            time.sleep(1)  # Generate one data point per second
    except Exception as e:
        logger.error(f"Error in data generator thread: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Signal that we're done
        data_queue.put(None)
        logger.info("Data generation completed")

def main():
    """Main function to run the visualization test."""
    logger.info("Starting visualization test")
    
    # Create visualizer
    visualizer = SensorDataVisualizer()
    
    # Start data generator thread
    generator = threading.Thread(target=data_generator_thread)
    generator.daemon = True
    generator.start()
    logger.info("Started data generator thread")
    
    # Keep the main thread running to process data
    def update_callback(frame):
        try:
            # Check if there's new data (non-blocking)
            if not data_queue.empty():
                data = data_queue.get_nowait()
                if data is None:
                    logger.info("Received end signal")
                    return False  # Stop animation
                visualizer.update_data(data)
            return True
        except Exception as e:
            logger.error(f"Error in update callback: {e}")
            return False
    
    # Override the animation update function
    visualizer._user_update_callback = update_callback
    
    # Start visualization in main thread
    logger.info("Starting visualization in main thread")
    visualizer.start()
    
    # Cleanup
    logger.info("Visualization test completed")
    if generator.is_alive():
        logger.info("Waiting for generator thread to finish...")
        generator.join(timeout=2)

if __name__ == "__main__":
    main() 