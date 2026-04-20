/* 
  Sleep Monitor with multiple sensors connected to MQTT server
*/

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <SPI.h>
#include <Adafruit_BME280.h>
#include <driver/i2s.h>
#include <PubSubClient.h>  // MQTT client library
#include <WiFi.h>  // ESP32 WiFi library

#define REPORTING_PERIOD_MS 3000
#define SOUND_WINDOW_MS 20  // Window to track min/max sound values (20 milliseconds)
#define INTENSITY_AVG_COUNT 10 // Number of intensity values to average
#define SAMPLE_COUNT 64

// WiFi credentials
// const char* ssid = "SPH-ASA";
// const char* password = "SPHasa25";
const char* ssid = "Redmmi-Aria";
const char* password = "robertwho";

// MQTT Settings
const char* mqtt_server = "broker.mqtt.cool";
const int mqtt_port = 1883;
const char* mqtt_topic = "minori-blanket-sensors";

// Board-specific pin configuration
#if CONFIG_IDF_TARGET_ESP32
const int lightPin = 34;
const int SDA_PIN = 21;
const int SCL_PIN = 22;
const int I2S_WS = 15;   // INMP441 LRCK / WS
const int I2S_SD = 32;   // INMP441 SD / DOUT
const int I2S_SCK = 14;  // INMP441 BCLK / SCK
#elif CONFIG_IDF_TARGET_ESP32C3
const int lightPin = 5;
const int SDA_PIN = 8;
const int SCL_PIN = 9;
const int I2S_WS = 3;   // INMP441 LRCK / WS
const int I2S_SD = 2;   // INMP441 SD / DOUT
const int I2S_SCK = 4;  // INMP441 BCLK / SCK
#else
  #error "Unsupported board target for this sketch"
#endif

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
int soundMin = 32767;   // Start with max 16-bit value
int soundMax = -32768;  // Start with min 16-bit value
int soundIntensity = 0; // Calculated sound intensity
int intensityValues[INTENSITY_AVG_COUNT]; // Array to store intensity values for averaging
int intensityIndex = 0; // Current index in the intensity array
int avgSoundIntensity = 0; // Averaged sound intensity
int lastWindowMin = 0;
int lastWindowMax = 0;

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

bool setupINMP441() {
  const i2s_config_t i2s_config = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = 16000,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_ONLY_LEFT,
    .communication_format = I2S_COMM_FORMAT_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = SAMPLE_COUNT,
    .use_apll = false,
    .tx_desc_auto_clear = false,
    .fixed_mclk = 0
  };

  const i2s_pin_config_t pin_config = {
    .bck_io_num = I2S_SCK,
    .ws_io_num = I2S_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_SD
  };

  esp_err_t err = i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
  if (err != ESP_OK) {
    Serial.printf("i2s_driver_install failed: %d\n", (int)err);
    return false;
  }

  err = i2s_set_pin(I2S_NUM_0, &pin_config);
  if (err != ESP_OK) {
    Serial.printf("i2s_set_pin failed: %d\n", (int)err);
    return false;
  }

  i2s_zero_dma_buffer(I2S_NUM_0);
  return true;
}

bool readINMP441Samples(int16_t* samples, size_t sampleCount, size_t* outSamplesRead) {
  size_t bytesRead = 0;
  const esp_err_t err = i2s_read(
    I2S_NUM_0,
    samples,
    sampleCount * sizeof(int16_t),
    &bytesRead,
    pdMS_TO_TICKS(10)
  );

  if (err != ESP_OK) {
    *outSamplesRead = 0;
    return false;
  }

  *outSamplesRead = bytesRead / sizeof(int16_t);
  return (*outSamplesRead > 0);
}

void setup() {
  pinMode(lightPin, INPUT);
  Serial.begin(115200);
  
  // This delay gives the chance to wait for a Serial Monitor without blocking if none is found
  delay(1500);
  
  // Initialize I2C with ESP32 pins
  Wire.begin(SDA_PIN, SCL_PIN);

  Serial.println("Initializing INMP441...");
  if (!setupINMP441()) {
    Serial.println("Failed to initialize INMP441");
    while (1) {
      delay(10);
    }
  }
  Serial.println("INMP441 ready!");
  
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
  
  // Continuously monitor INMP441 levels to track min/max sample values.
  int16_t samples[SAMPLE_COUNT];
  size_t sampleRead = 0;
  if (readINMP441Samples(samples, SAMPLE_COUNT, &sampleRead)) {
    for (size_t i = 0; i < sampleRead; i++) {
      if (samples[i] < soundMin) {
        soundMin = samples[i];
      }
      if (samples[i] > soundMax) {
        soundMax = samples[i];
      }
    }
  }
  
  // Reset the min/max values periodically
  if (millis() - tsSoundWindow > SOUND_WINDOW_MS) {
    if (soundMax >= soundMin) {
      // Use peak-to-peak value as sound intensity.
      soundIntensity = soundMax - soundMin;
      lastWindowMin = soundMin;
      lastWindowMax = soundMax;
    } else {
      soundIntensity = 0;
      lastWindowMin = 0;
      lastWindowMax = 0;
    }
    
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
    soundMin = 32767;
    soundMax = -32768;
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
    Serial.print("INMP441 window min: ");
    Serial.print(lastWindowMin);
    Serial.print("\twindow max: ");
    Serial.print(lastWindowMax);
    Serial.print("\tPeak-to-peak: ");
    Serial.print(soundIntensity);
    Serial.print("\tAvg intensity: ");
    Serial.println(avgSoundIntensity);
    Serial.print("Light intensity: ");
    Serial.println(light);

    Serial.println("============================");
    
    tsLastReport = millis();
  }
}