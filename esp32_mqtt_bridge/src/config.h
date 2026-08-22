// ============================================================================
// config.h — Per‑mesh BRMesh bridge configuration.
//
// Pick exactly ONE mesh by defining `BRMESH_MESH` on the compiler command line
// (in platformio.ini under [env] build_flags) BEFORE this file is included.
//   pio run -e esp32s3 -D BRMESH_MESH=BRMESH_PAUL
//
// Each mesh entry supplies everything the bridge needs to talk to that mesh:
//   - the 4‑byte mesh key (all commands are encrypted with it)
//   - MQTT broker / WiFi credentials used by the Home‑Assistant entity bridge
//   - the preconfigured light fixtures already paired into that mesh
//   - the 128‑bit beacon UUID (identifies the bridge on the air)
//
// To add a new mesh, add a new `#elif BRMESH_MESH == ...` block below.
// ============================================================================
#ifndef BRMESH_CONFIG_H
#define BRMESH_CONFIG_H

// Mesh selectors. Pick one via platformio.ini build_flags:
//   -D BRMESH_MESH=BRMESH_OLD
//   -D BRMESH_MESH=BRMESH_PAUL
#if !defined(BRMESH_OLD)
  #define BRMESH_OLD  1
#endif
#if !defined(BRMESH_PAUL)
  #define BRMESH_PAUL 2
#endif

#ifndef BRMESH_MESH
#error "Define BRMESH_MESH in platformio.ini (build_flags) — e.g. -D BRMESH_MESH=BRMESH_PAUL"
#endif

// ---------------------------------------------------------------------------
// Example mesh A. Replace the placeholder key and WiFi with your own values.
// The default fixtures are example RGBW floodlights.
// ---------------------------------------------------------------------------
#if BRMESH_MESH == BRMESH_OLD

  #define MESH_NAME            "example-mesh-a"

  // 4‑byte mesh key (hex bytes, MSB first as read from the setup).
  #define USE_RANDOM_KEY 0
  #define MY_KEY_0 0x12
  #define MY_KEY_1 0x34
  #define MY_KEY_2 0x56
  #define MY_KEY_3 0x78

  // MQTT broker (Home Assistant / mosquitto) + WiFi.
  #define MQTT_BROKER_ADDR IPAddress(192,168,1,10)   // replace with your broker
  #define WIFI_SSID "YOUR_WIFI_SSID"
  #define WIFI_PASS "YOUR_WIFI_PASSWORD"

  // Preconfigured lights already in this mesh. `number` is the device number;
  // type {0xa1,0xa8} = RGBW floodlight. ids are unique per light (HA entity).
  // 0 entries + PRECONFIGURED_LIGHT_COUNT 0 -> fall back to auto-discovery.
  #define PRECONFIGURED_LIGHT_COUNT 6
  const PreconfiguredLight preconfiguredLights[] = {
    { 1, {0xa1, 0xa8}, "e242", "Light_1" },
    { 2, {0xa1, 0xa8}, "564e", "Light_2" },
    { 3, {0xa1, 0xa8}, "4667", "Light_3" },
    { 4, {0xa1, 0xa8}, "7b5d", "Light_4" },
    { 5, {0xa1, 0xa8}, "3c7d", "Light_5" },
    { 6, {0xa1, 0xa8}, "ff59", "Light_6" },
  };

  // 128‑bit beacon UUID for this bridge.
  #define BEACON_UUID "a1885535-7e56-4c9c-ae19-796ce9864f3f"

// ---------------------------------------------------------------------------
// Example mesh B. The plaintext group bytes stay 43 2A A8 00 (80|00);
// only the key differs. Replace the placeholder key/fixtures with your own.
// ---------------------------------------------------------------------------
#elif BRMESH_MESH == BRMESH_PAUL

  #define MESH_NAME            "example-mesh-b"

  // 4‑byte mesh key (placeholder 12345678 — replace with your own mesh key).
  #define USE_RANDOM_KEY 0
  #define MY_KEY_0 0x12
  #define MY_KEY_1 0x34
  #define MY_KEY_2 0x56
  #define MY_KEY_3 0x78

  // MQTT broker (Home Assistant / mosquitto) + WiFi.
  #define MQTT_BROKER_ADDR IPAddress(192,168,1,10)   // replace with your broker
  #define WIFI_SSID "YOUR_WIFI_SSID"
  #define WIFI_PASS "YOUR_WIFI_PASSWORD"

  // Example fixtures. `number` is the device number seen in your sniff/ADB
  // capture; 0x2A (42) is the "All" group id and is excluded here because it's
  // the group switch, not a single light. All type {0xa1,0xa8} = RGBW.
  #define PRECONFIGURED_LIGHT_COUNT 41
  const PreconfiguredLight preconfiguredLights[] = {
    { 0x02, {0xa1, 0xa8}, "02",  "Light_02" },
    { 0x03, {0xa1, 0xa8}, "03",  "Light_03" },
    { 0x05, {0xa1, 0xa8}, "05",  "Light_05" },
    { 0x07, {0xa1, 0xa8}, "07",  "Light_07" },
    { 0x09, {0xa1, 0xa8}, "09",  "Light_09" },
    { 0x0A, {0xa1, 0xa8}, "0a",  "Light_0a" },
    { 0x0D, {0xa1, 0xa8}, "0d",  "Light_0d" },
    { 0x13, {0xa1, 0xa8}, "13",  "Light_13" },
    { 0x14, {0xa1, 0xa8}, "14",  "Light_14" },
    { 0x18, {0xa1, 0xa8}, "18",  "Light_18" },
    { 0x19, {0xa1, 0xa8}, "19",  "Light_19" },
    { 0x1A, {0xa1, 0xa8}, "1a",  "Light_1a" },
    { 0x1B, {0xa1, 0xa8}, "1b",  "Light_1b" },
    { 0x1D, {0xa1, 0xa8}, "1d",  "Light_1d" },
    { 0x1E, {0xa1, 0xa8}, "1e",  "Light_1e" },
    { 0x20, {0xa1, 0xa8}, "20",  "Light_20" },
    { 0x24, {0xa1, 0xa8}, "24",  "Light_24" },
    { 0x25, {0xa1, 0xa8}, "25",  "Light_25" },
    { 0x26, {0xa1, 0xa8}, "26",  "Light_26" },
    { 0x27, {0xa1, 0xa8}, "27",  "Light_27" },
    { 0x2D, {0xa1, 0xa8}, "2d",  "Light_2d" },
    { 0x2E, {0xa1, 0xa8}, "2e",  "Light_2e" },
    { 0x2F, {0xa1, 0xa8}, "2f",  "Light_2f" },
    { 0x30, {0xa1, 0xa8}, "30",  "Light_30" },
    { 0x31, {0xa1, 0xa8}, "31",  "Light_31" },
    { 0x32, {0xa1, 0xa8}, "32",  "Light_32" },
    { 0x33, {0xa1, 0xa8}, "33",  "Light_33" },
    { 0x34, {0xa1, 0xa8}, "34",  "Light_34" },
    { 0x35, {0xa1, 0xa8}, "35",  "Light_35" },
    { 0x36, {0xa1, 0xa8}, "36",  "Light_36" },
    { 0x37, {0xa1, 0xa8}, "37",  "Light_37" },
    { 0x38, {0xa1, 0xa8}, "38",  "Light_38" },
    { 0x39, {0xa1, 0xa8}, "39",  "Light_39" },
    { 0x3A, {0xa1, 0xa8}, "3a",  "Light_3a" },
    { 0x3B, {0xa1, 0xa8}, "3b",  "Light_3b" },
    { 0x3C, {0xa1, 0xa8}, "3c",  "Light_3c" },
    { 0x3D, {0xa1, 0xa8}, "3d",  "Light_3d" },
    { 0x3E, {0xa1, 0xa8}, "3e",  "Light_3e" },
    { 0x3F, {0xa1, 0xa8}, "3f",  "Light_3f" },
    { 0x40, {0xa1, 0xa8}, "40",  "Light_40" },
    { 0x48, {0xa1, 0xa8}, "48",  "Light_48" },
  };

  #define BEACON_UUID "a1885535-7e56-4c9c-ae19-796ce9864f3f"

#else
  #error "Unknown BRMESH_MESH — add a matching #elif block in config.h"
#endif

#endif // BRMESH_CONFIG_H
