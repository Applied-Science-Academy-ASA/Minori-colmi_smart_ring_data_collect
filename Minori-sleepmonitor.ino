/* 
  Sleep Monitor with multiple sensors connected to MQTT server
*/

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <SPI.h>
#include <Adafruit_BME280.h>
#include <PubSubClient.h>  // MQTT client library
#include <WiFi.h>  // ESP32 WiFi library

#define soundPin 35
#define lightPin 34
#define REPORTING_PERIOD_MS 3000
#define SOUND_WINDOW_MS 20  // Window to track min/max sound values (20 milliseconds)
#define INTENSITY_AVG_COUNT 10 // Number of intensity values to average

// WiFi credentials
// const char* ssid = "SPH-ASA";
// const char* password = "SPHasa25";
const char* ssid = "Redmmi";
const char* password = "robertwho";

// MQTT Settings
const char* mqtt_server = "broker.mqtt.cool";
const int mqtt_port = 1883;
const char* mqtt_topic = "minori-blanket-sensors";

// ESP32 pin configuration for I2C
const int SDA_PIN = 21;  // I2C SDA pin
const int SCL_PIN = 22;  // I2C SCL pin

// Sensor variables
float temperature = 0;
float humidity = 0;
int sound = 0;
int light = 0;
float movement = 0;
int heartrate = 0;
int oxygen = 0;

Adafruit_MPU6050 mpu;
Adafruit_BME280 bme; // use I2C interface
Adafruit_Sensor *bme_temp = bme.getTemperatureSensor();
Adafruit_Sensor *bme_pressure = bme.getPressureSensor();
Adafruit_Sensor *bme_humidity = bme.getHumiditySensor();
WiFiClient espClient;
PubSubClient mqttClient(espClient);
uint32_t tsLastReport = 0;
uint32_t tsSoundWindow = 0;

// Sound intensity tracking variables
int soundMin = 4095;  // Start with max ADC value
int soundMax = 0;     // Start with min ADC value
int soundIntensity = 0; // Calculated sound intensity
int intensityValues[INTENSITY_AVG_COUNT]; // Array to store intensity values for averaging
int intensityIndex = 0; // Current index in the intensity array
int avgSoundIntensity = 0; // Averaged sound intensity

// Variable to calculate movement from accelerometer data
float prevAccelX = 0, prevAccelY = 0, prevAccelZ = 0;

// Function to reconnect to MQTT server
void reconnectMQTT() {
  // Loop until we're reconnected
  while (!mqttClient.connected()) {
    Serial.print("Attempting MQTT connection...");
    // Create a random client ID
    String clientId = "ESP32Client-";
    clientId += String(random(0xffff), HEX);
    // Attempt to connect
    if (mqttClient.connect(clientId.c_str())) {
      Serial.println("connected");
    } else {
      Serial.print("failed, rc=");
      Serial.print(mqttClient.state());
      Serial.println(" try again in 5 seconds");
      // Wait 5 seconds before retrying
      delay(5000);
    }
  }
}

void setup() {
  pinMode(soundPin, INPUT);
  pinMode(lightPin, INPUT);
  Serial.begin(115200);
  
  // This delay gives the chance to wait for a Serial Monitor without blocking if none is found
  delay(1500);
  
  // Initialize I2C with ESP32 pins
  Wire.begin(SDA_PIN, SCL_PIN);
  
  // Connect to WiFi
  Serial.println();
  Serial.print("Connecting to ");
  Serial.println(ssid);
  
  WiFi.begin(ssid, password);
  
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  
  Serial.println("");
  Serial.println("WiFi connected");
  Serial.println("IP address: ");
  Serial.println(WiFi.localIP());
  
  // Initialize MPU6050
  Serial.println("Initializing MPU6050...");
  if (!mpu.begin()) {
    Serial.println("Failed to find MPU6050 chip");
    while (1) {
      delay(10);
    }
  }
  Serial.println("MPU6050 Found!");

  // Set accelerometer range to +-8G
  mpu.setAccelerometerRange(MPU6050_RANGE_8_G);
  
  // Set gyro range to +- 500 deg/s
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  
  // Set filter bandwidth to 21 Hz
  mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  
  // Initialize BME280
  Serial.println("Initializing BME280...");
  if (!bme.begin(0x76)) {
    Serial.println("Could not find a valid BME280 sensor, check wiring!");
    while (1) delay(10);
  }
  Serial.println("BME280 Found!");
  
  // Setup MQTT client
  mqttClient.setServer(mqtt_server, mqtt_port);
  
  Serial.println("Setup complete. All sensors initialized!");
}

void loop() {
  // Check MQTT connection
  // if (!mqttClient.connected()) {
  //   reconnectMQTT();
  // }
  // mqttClient.loop();
  
  // Continuously monitor sound levels to track min/max
  int currentSoundValue = analogRead(soundPin);
  
  // Update min/max sound values
  if (currentSoundValue < soundMin) {
    soundMin = currentSoundValue;
  }
  if (currentSoundValue > soundMax) {
    soundMax = currentSoundValue;
  }
  
  // Reset the min/max values periodically
  if (millis() - tsSoundWindow > SOUND_WINDOW_MS) {
    // Calculate sound intensity as the range between min and max
    soundIntensity = soundMax - soundMin;
    
    // Add to moving average array
    intensityValues[intensityIndex] = soundIntensity;
    intensityIndex = (intensityIndex + 1) % INTENSITY_AVG_COUNT;
    
    // Calculate the average intensity
    long sum = 0;
    for (int i = 0; i < INTENSITY_AVG_COUNT; i++) {
      sum += intensityValues[i];
    }
    avgSoundIntensity = sum / INTENSITY_AVG_COUNT;
    
    tsSoundWindow = millis();
    
    // Reset min/max for next window
    soundMin = 4095;
    soundMax = 0;
  }
  
  // Read sensor data and update cloud variables
  if (millis() - tsLastReport > REPORTING_PERIOD_MS) {
    // Read MPU6050 data
    sensors_event_t a, g, temp;
    mpu.getEvent(&a, &g, &temp);
    
    // Calculate movement (change in acceleration)
    float deltaX = abs(a.acceleration.x - prevAccelX);
    float deltaY = abs(a.acceleration.y - prevAccelY);
    float deltaZ = abs(a.acceleration.z - prevAccelZ);
    
    // Use maximum value of the three axes as movement value
    float movementValue = max(deltaX, max(deltaY, deltaZ));
    
    // Update previous acceleration values
    prevAccelX = a.acceleration.x;
    prevAccelY = a.acceleration.y;
    prevAccelZ = a.acceleration.z;
    
    // Read BME280 data
    sensors_event_t temp_event, pressure_event, humidity_event;
    bme_temp->getEvent(&temp_event);
    bme_pressure->getEvent(&pressure_event);
    bme_humidity->getEvent(&humidity_event);
    
    // Read light data
    int lightValue = analogRead(lightPin);
    
    // Update sensor variables
    temperature = temp_event.temperature;
    humidity = humidity_event.relative_humidity;
    sound = avgSoundIntensity;  // Use the averaged sound intensity
    light = lightValue;
    movement = movementValue;
    oxygen = 0;  // Setting oxygen to 0 as we no longer have the sensor
    
    // Prepare JSON payload for MQTT
    char mqttPayload[256];
    snprintf(mqttPayload, sizeof(mqttPayload), 
      "{\"sound\":%d,\"light\":%d,\"movement\":%.2f,\"temperature\":%.2f,\"humidity\":%.2f}",
      sound, light, movement, temperature, humidity);
    
    // Publish to MQTT
    mqttClient.publish(mqtt_topic, mqttPayload);
    
    // Print all values to Serial
    Serial.println("\n=== SENSOR READINGS ===");
    
    // Print MPU6050 data
    Serial.println("------ MPU6050 DATA ------");
    Serial.print("Acceleration X: ");
    Serial.print(a.acceleration.x);
    Serial.print(", Y: ");
    Serial.print(a.acceleration.y);
    Serial.print(", Z: ");
    Serial.print(a.acceleration.z);
    Serial.println(" m/s^2");
    Serial.print("Movement value: ");
    Serial.println(movementValue);

    Serial.print("Rotation X: ");
    Serial.print(g.gyro.x);
    Serial.print(", Y: ");
    Serial.print(g.gyro.y);
    Serial.print(", Z: ");
    Serial.print(g.gyro.z);
    Serial.println(" rad/s");
    
    Serial.print("Temperature (MPU): ");
    Serial.print(temp.temperature);
    Serial.println(" degC");
    
    // Print BME280 data
    Serial.println("------ BME280 DATA ------");
    Serial.print("Temperature: ");
    Serial.print(temperature);
    Serial.println(" *C");

    Serial.print("Humidity: ");
    Serial.print(humidity);
    Serial.println(" %");

    Serial.print("Pressure: ");
    Serial.print(pressure_event.pressure);
    Serial.println(" hPa");
    
    // Print MQTT data
    Serial.println("------ MQTT DATA ------");
    Serial.print("MQTT Payload: ");
    Serial.println(mqttPayload);
    
    // Print sound and light data
    Serial.println("------ SOUND & LIGHT DATA ------");
    Serial.print("Sound raw value: ");
    Serial.print(currentSoundValue);
    Serial.print("\tSound min: ");
    Serial.print(soundMin);
    Serial.print("\tSound max: ");
    Serial.print(soundMax);
    Serial.print("\tSound intensity: ");
    Serial.print(soundIntensity);
    Serial.print("\tAvg intensity: ");
    Serial.println(avgSoundIntensity);
    Serial.print("Light intensity: ");
    Serial.println(light);

    Serial.println("============================");
    
    tsLastReport = millis();
  }
}