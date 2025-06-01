import matplotlib.pyplot as plt
import matplotlib.animation as animation
import numpy as np
import json
import time
from datetime import datetime
from typing import Dict, List, Any, Optional
from collections import deque
import threading
import logging

logger = logging.getLogger(__name__)

class SensorDataVisualizer:
    """Real-time visualization of sensor data with 6 line graphs."""
    
    def __init__(self, max_points: int = 100):
        """
        Initialize the visualizer with the specified history length.
        
        Args:
            max_points: Maximum number of data points to display in history
        """
        self.max_points = max_points
        self.running = False
        self.fig = None
        self.axes = None
        self.update_count = 0
        self._user_update_callback = None  # Custom user callback
        
        # Initialize data storage
        self.timestamps = deque(maxlen=max_points)
        self.data = {
            "heartrate": deque(maxlen=max_points),
            "sound": deque(maxlen=max_points),
            "light": deque(maxlen=max_points),
            "movement": deque(maxlen=max_points),
            "temperature": deque(maxlen=max_points),
            "humidity": deque(maxlen=max_points)
        }
        
        # Initialize with some dummy data to avoid empty plots
        current_time = datetime.now()
        self.timestamps.append(current_time)
        for key in self.data:
            self.data[key].append(None)
        
        # Default colors for each data type
        self.colors = {
            "heartrate": "red",
            "sound": "blue",
            "light": "orange",
            "movement": "green",
            "temperature": "purple",
            "humidity": "teal"
        }
        
        # Line objects for each data type
        self.lines = {}
        
        # Lock for thread safety
        self.lock = threading.Lock()
        
        logger.info("SensorDataVisualizer initialized")
        
    def setup_plot(self):
        """Set up the matplotlib figure and axes."""
        logger.info("Setting up visualization plot")
        plt.style.use('dark_background')  # Use dark theme for better visibility
        
        # Create figure and subplot grid (3x2)
        self.fig, self.axes = plt.subplots(3, 2, figsize=(12, 8))
        self.fig.tight_layout(pad=3.0)
        self.fig.canvas.manager.set_window_title('Sensor Data Visualization')
        
        # Flatten axes array for easier access
        self.axes = self.axes.flatten()
        
        # Set up each subplot
        data_types = list(self.data.keys())
        for i, data_type in enumerate(data_types):
            ax = self.axes[i]
            color = self.colors[data_type]
            
            # Create empty line
            line, = ax.plot([], [], lw=2, color=color)
            self.lines[data_type] = line
            
            # Set labels and title
            ax.set_xlabel('Time')
            ax.set_ylabel(self._get_unit(data_type))
            ax.set_title(data_type.capitalize())
            ax.grid(True, alpha=0.3)
            
            # Set initial y-limits based on expected data ranges
            ax.set_ylim(self._get_y_limits(data_type))
            
            # Set initial x-limits
            ax.set_xlim(0, 10)
        
        # Add timestamp and status to the figure
        self.timestamp_text = self.fig.text(0.5, 0.01, '', ha='center')
        self.status_text = self.fig.text(0.01, 0.01, 'Waiting for data...', ha='left')
        
        # Adjust spacing
        self.fig.tight_layout(rect=[0, 0.03, 1, 0.95])
        logger.info("Plot setup complete")
        
    def _get_unit(self, data_type: str) -> str:
        """Return the appropriate unit for each data type."""
        units = {
            "heartrate": "BPM",
            "sound": "dB",
            "light": "Lux",
            "movement": "g",
            "temperature": "°C",
            "humidity": "%"
        }
        return units.get(data_type, "")
    
    def _get_y_limits(self, data_type: str) -> tuple:
        """Return appropriate y-axis limits for each data type."""
        limits = {
            "heartrate": (40, 180),
            "sound": (0, 100),
            "light": (0, 1000),
            "movement": (0, 2),
            "temperature": (15, 35),
            "humidity": (0, 100)
        }
        return limits.get(data_type, (0, 100))
    
    def update_data(self, new_data: Dict[str, Any]):
        """
        Update the visualization with new sensor data.
        
        Args:
            new_data: Dictionary containing sensor data values
        """
        if not self.running:
            logger.warning("Attempted to update data but visualizer is not running")
            return
            
        with self.lock:
            try:
                # Add current timestamp
                current_time = datetime.now()
                self.timestamps.append(current_time)
                
                self.update_count += 1
                logger.info(f"Updating visualization data (update #{self.update_count}): {new_data}")
                
                # Update each data series with new values or None if missing
                for data_type in self.data:
                    if data_type in new_data and new_data[data_type] is not None:
                        try:
                            value = float(new_data[data_type])
                            self.data[data_type].append(value)
                            logger.debug(f"Added {data_type} = {value}")
                        except (ValueError, TypeError):
                            logger.warning(f"Invalid {data_type} value: {new_data[data_type]}")
                            self.data[data_type].append(None)
                    else:
                        # If data is missing, append None to maintain alignment
                        self.data[data_type].append(None)
                        logger.debug(f"No data for {data_type}")
                
                logger.debug(f"Current data sizes: {[len(self.data[k]) for k in self.data]}")
            except Exception as e:
                logger.error(f"Error updating visualization data: {e}")
                import traceback
                traceback.print_exc()
    
    def _update_plot(self, frame):
        """Update function for matplotlib animation."""
        # Call user callback if provided
        if self._user_update_callback:
            continue_updates = self._user_update_callback(frame)
            if continue_updates is False:
                logger.info("User callback requested animation to stop")
                self.stop()
                return list(self.lines.values())
                
        with self.lock:
            try:
                # Convert timestamps to relative seconds for x-axis
                if not self.timestamps:
                    return list(self.lines.values())
                    
                x_data = [(t - self.timestamps[0]).total_seconds() for t in self.timestamps]
                
                # Update status text
                self.status_text.set_text(f'Updates: {self.update_count}')
                
                # Update each line with new data
                for data_type, line in self.lines.items():
                    # Remove None values for plotting but keep indices aligned
                    valid_indices = [i for i, val in enumerate(self.data[data_type]) if val is not None]
                    valid_x = [x_data[i] for i in valid_indices]
                    valid_y = [self.data[data_type][i] for i in valid_indices]
                    
                    # Update line data
                    line.set_data(valid_x, valid_y)
                    
                    # Adjust x-axis limits to show all data
                    ax = line.axes
                    if valid_x:
                        ax.set_xlim(0, max(x_data) + 1)
                    
                    # Dynamically adjust y-axis if data exceeds current limits
                    if valid_y:
                        ymin, ymax = ax.get_ylim()
                        data_min, data_max = min(valid_y), max(valid_y)
                        
                        # Add padding to limits
                        if data_min < ymin:
                            ax.set_ylim(bottom=data_min * 0.9)
                        if data_max > ymax:
                            ax.set_ylim(top=data_max * 1.1)
                
                # Update timestamp
                if self.timestamps:
                    self.timestamp_text.set_text(f'Last update: {self.timestamps[-1].strftime("%H:%M:%S")}')
                    
                # Force redraw
                self.fig.canvas.draw_idle()
                
                return list(self.lines.values())
            except Exception as e:
                logger.error(f"Error updating plot: {e}")
                import traceback
                traceback.print_exc()
                return list(self.lines.values())
    
    def start(self):
        """Start the visualization."""
        if self.running:
            logger.warning("Visualization is already running")
            return
            
        logger.info("Starting visualization")
        self.running = True
        
        # Set up the plot if not done already
        if self.fig is None:
            self.setup_plot()
        
        # Create animation
        self.ani = animation.FuncAnimation(
            self.fig, 
            self._update_plot, 
            interval=100,  # Update every 100ms
            blit=True
        )
        
        # Show the plot (this will block until the window is closed)
        logger.info("Showing visualization plot window")
        plt.show()
        logger.info("Visualization window closed")
        
    def stop(self):
        """Stop the visualization."""
        if not self.running:
            return
            
        logger.info("Stopping visualization")
        self.running = False
        if hasattr(self, 'ani'):
            self.ani.event_source.stop()
            
        # Close the figure
        if self.fig is not None:
            plt.close(self.fig)
            self.fig = None
        
        logger.info("Visualization stopped")


# Function to parse JSON from serial or MQTT
def parse_sensor_data(data_str: str) -> Optional[Dict[str, Any]]:
    """Parse a JSON string into a dictionary of sensor data."""
    try:
        data = json.loads(data_str)
        return data
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON data: {data_str}")
        return None


# Example usage:
# visualizer = SensorDataVisualizer()
# visualizer.start()  # This blocks until the window is closed

# In another thread:
# data = parse_sensor_data('{"sound":50,"light":200,"movement":0.5,"heartrate":75,"temperature":22.5,"humidity":45.3}')
# visualizer.update_data(data) 