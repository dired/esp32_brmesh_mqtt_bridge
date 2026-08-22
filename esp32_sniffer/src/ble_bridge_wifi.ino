/*
 * ble_bridge_wifi.ino — Passive BRMesh BLE sniffer -> WiFi/UDP bridge
 *
 * Same passive sniffer as the original serial sniffer, but each forwarded `BLE|...`
 * line is ALSO broadcast as a UDP datagram to the local subnet, and the board
 * announces itself via mDNS so the host can discover it by name. Serial output
 * is kept too (for USB debugging).
 *
 *   HOST RECEIVER:  python3 monitor_remote.py
 *     - listens on UDP <UDP_PORT> (default 41234)
 *     - accepts a --host to unicast to instead of broadcast (optional)
 *
 * LINE FORMAT (same as the serial version, machine-parseable):
 *   BLE|MAC=<mac>|RSSI=<rssi>|LEN=<len>|DATA=<hex>|UPTIME=<ms>
 *
 * BUILD/FLASH (from this directory):
 *   pio run -e esp32s3 -t upload
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <ESPmDNS.h>
#include <BLEDevice.h>
#include <BLEUtils.h>
#include <BLEScan.h>
#include "esp_bt.h"                 // esp_bt_controller_mem_release (classic ESP32)
#include "esp_bt_device.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

// --------------------------------------------------------------- CONFIG ----
// >>> FILL THESE IN <<<
#define WIFI_SSID   "YOUR_WIFI_SSID"
#define WIFI_PASS   "YOUR_WIFI_PASSWORD"

#define UDP_PORT      41234          // host must listen on this port
#define UDP_TTL       1              // 0 = don't set TTL (may not cross routers)
#define MDNS_HOST     "brmesh-sniffer"  // -> brmesh-sniffer.local

#define SERIAL_BAUD   115200
#define SNIFF_RAW_DEBUG 0          // 1 = forward EVERY advert (active scan)

// Onboard LED: GPIO2 on most ESP32 devkits (active-low). Change if your board
// differs (ESP32-C3/S3 use an RGB LED on a different pin).
#define LED_PIN       2
#define LED_ACTIVE_LOW 1           // 1 = LED on when pin LOW (devkit), 0 = HIGH

// BRMesh / IoT OUIs we care about (legacy scan filter).
static const char *kMeshOuis[] = {
    "a4:c1:38", "50:ec:50", "08:3a:f2", "e8:db:84",
};
static const size_t kMeshOuiCount = sizeof(kMeshOuis) / sizeof(kMeshOuis[0]);

#define HEARTBEAT_INTERVAL_MS 10000UL
#define SCAN_INTERVAL_MS 100
#define SCAN_WINDOW_MS 100

static BLEScan *g_scan = nullptr;
static WiFiUDP g_udp;
static unsigned long g_lastOutputMs = 0;

// ------------------------------------------------------------- LED helpers --
static inline void ledOn()  { digitalWrite(LED_PIN, LED_ACTIVE_LOW ? LOW : HIGH); }
static inline void ledOff() { digitalWrite(LED_PIN, LED_ACTIVE_LOW ? HIGH : LOW); }

// Blink #times, ~80ms per toggle, then leave the LED off.
static void ledBlink(unsigned int times) {
  for (unsigned int i = 0; i < times * 2; i++) {
    (i & 1) ? ledOff() : ledOn();
    delay(80);
  }
  ledOff();
}

// ------------------------------------------------------------- forwarding --
// Send one line to serial (USB) and as a UDP datagram to the whole subnet.
static void forward(const char *line) {
  Serial.print(line);

  // Broadcast on the same port so any listener on the LAN sees it.
  g_udp.beginPacket(IPAddress(255, 255, 255, 255), UDP_PORT);
  g_udp.write((const uint8_t *)line, strlen(line));
  g_udp.endPacket();
}

static bool shouldKeep(const uint8_t *payload, size_t len, const char *macLower) {
#if SNIFF_RAW_DEBUG
  (void)payload; (void)len; (void)macLower;
  return true;
#else
  for (size_t i = 0; i + 2 < len; i++) {
    if (payload[i] == 0xFF && payload[i + 1] == 0xF0 && payload[i + 2] == 0xFF)
      return true;
  }
  for (size_t i = 0; i < kMeshOuiCount; i++) {
    if (strncmp(macLower, kMeshOuis[i], strlen(kMeshOuis[i])) == 0) return true;
  }
  return false;
#endif
}

class SnifferCallback : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) override {
    char mac[18];
    const char *addr = advertisedDevice.getAddress().toString().c_str();
    strncpy(mac, addr, sizeof(mac) - 1);
    mac[sizeof(mac) - 1] = '\0';
    for (char *p = mac; *p; p++) {
      if (*p >= 'A' && *p <= 'Z') *p = *p - 'A' + 'a';
    }

    uint8_t *payload = advertisedDevice.getPayload();
    size_t len = advertisedDevice.getPayloadLength();
    int rssi = advertisedDevice.getRSSI();

    if (!shouldKeep(payload, len, mac)) return;

    const char *prefix = (kMeshOuiCount == 0) ? "UNK|" : "BLE|";

    // Build the line into a buffer (also sent over UDP as one datagram).
    char line[256];
    size_t o = 0;
    o += snprintf(line + o, sizeof(line) - o, "%sMAC=%s|RSSI=%d|LEN=%u|DATA=",
                  prefix, mac, rssi, (unsigned)len);
    for (size_t i = 0; i < len && o < sizeof(line) - 3; i++) {
      o += snprintf(line + o, sizeof(line) - o, "%02X", payload[i]);
    }
    o += snprintf(line + o, sizeof(line) - o, "|UPTIME=%lu\n", (unsigned long)millis());

    forward(line);
    // Short flash: a valid BRMesh packet just arrived on the air.
    ledBlink(1);
    g_lastOutputMs = millis();
  }
};

static void scanTask(void *) {
  g_scan->start(0, false);
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(50);                      // let the USB CDC enumerate before printing

  // Onboard LED: on during bring-up, then blinks on heartbeats/packets.
  pinMode(LED_PIN, OUTPUT);
  ledOn();

  Serial.println("==============================================");
  Serial.println(" BRMesh Remote Sniffer (WiFi/UDP)");
  Serial.printf(" Firmware build: %s %s\n", __DATE__, __TIME__);
  Serial.println("----------------------------------------------");
  Serial.println(" BLE scan -> UDP broadcast -> monitor_remote.py");
  Serial.println("==============================================");

  // --- BLE passive scan ---
  // IMPORTANT: init BLE BEFORE WiFi. On the classic ESP32, WiFi + BLE sharing
  // the radio/RAM is tight, and Bluedroid needs a large contiguous heap for the
  // BTA structures. If WiFi connects first it fragments the heap and BLE's
  // bta_sys_init memset can crash (StoreProhibited, EXCVADDR=0). Starting BLE
  // first gives it clean heap. The passive scan runs in its own task, which is
  // independent of the WiFi connection.
  Serial.println("Starting BLE passive scan ...");

#if !CONFIG_IDF_TARGET_ESP32
  // ESP32-S3/C3: no Classic BT controller; nothing to release.
  Serial.println("  chip has no Classic BT; skipping mem release");
#else
  // Classic ESP32: release the Classic BT (BR/EDR) controller memory before BLE
  // init. Running WiFi + BLE on the original ESP32 is RAM-tight, and Classic BT
  // otherwise reserves ~150KB we don't need (we only use BLE).
  esp_err_t er = esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT);
  Serial.printf("  esp_bt_controller_mem_release(CLASSIC_BT) -> %s\n",
                er == ESP_OK ? "OK" : (er == ESP_ERR_INVALID_STATE
                                       ? "already released" : "not supported"));
#endif

  BLEDevice::init("");
  g_scan = BLEDevice::getScan();
  // wantDuplicates=true: without this, the Arduino BLEScan software duplicate
  // filter drops EVERY repeat advertisement from the same MAC after the first
  // sighting. Our bridge uses a FIXED MAC (cc:8d:a2:ec:bb:5d), so a repeated
  // `allon`/`alloff` burst would be forwarded only once and subsequent presses
  // would never appear. Requesting duplicates makes every frame (fixed OR
  // randomized MAC) reliably visible, which is what we need to verify the
  // bridge by MAC address.
  g_scan->setAdvertisedDeviceCallbacks(new SnifferCallback(), true /* wantDuplicates */);
#if SNIFF_RAW_DEBUG
  g_scan->setActiveScan(true);
  Serial.println("  mode: ACTIVE scan (RAW_DEBUG=1)");
#else
  g_scan->setActiveScan(false);
  Serial.println("  mode: PASSIVE scan (BRMesh filter only)");
#endif
  g_scan->setInterval(SCAN_INTERVAL_MS);
  g_scan->setWindow(SCAN_WINDOW_MS);
  xTaskCreate(scanTask, "brmesh_scan", 8192, NULL, 1, NULL);

  // --- WiFi ---
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(250);
    Serial.print('.');
  }
  Serial.printf("\nWiFi connected: %s  (MAC %s)\n",
                WiFi.localIP().toString().c_str(), WiFi.macAddress().c_str());

  if (MDNS.begin(MDNS_HOST)) {
    Serial.printf("mDNS started: %s.local\n", MDNS_HOST);
  } else {
    Serial.println("WARN: mDNS start failed");
  }
  g_udp.begin(UDP_PORT);
  Serial.printf("UDP broadcast -> port %d\n", UDP_PORT);

  g_lastOutputMs = millis();
  ledBlink(2);
  Serial.println("READY. Broadcasting BLE|... lines over UDP.");
}

void loop() {
  if (millis() - g_lastOutputMs > HEARTBEAT_INTERVAL_MS) {
    char hb[64];
    snprintf(hb, sizeof(hb), "BLE_HEARTBEAT|UPTIME=%lu\n", (unsigned long)millis());
    Serial.print(hb);                       // serial heartbeat
    g_udp.beginPacket(IPAddress(255, 255, 255, 255), UDP_PORT);
    g_udp.write((const uint8_t *)hb, strlen(hb));
    g_udp.endPacket();
    ledBlink(1);                            // heartbeat LED blink
    g_lastOutputMs = millis();
  }
  yield();
}
