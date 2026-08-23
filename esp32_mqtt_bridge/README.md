## Usage in this project

1. Fill in brmesh-key (from adb logcat), wifi credentials, and lights (from adb logcat or sniffing) in config.h
2. Flash on normal esp32 (s3 works as well) which will then be capable of emitting the right ble packets
3. Use mqtt or the gui in tools/brmesh_control_gui to control the lights

---

## Installation

It's PlatformIO based, built via VSCode. Download the source and it flash to an ESP32 device using PlatformIO.

By default the ESP32 partitions will be too small, so I've also included the partition table layout, which can also be flashed using PlatformIO.

## Configuration — per-mesh in `src/config.h`

All mesh-specific settings (BLE key, MQTT broker, WiFi, preconfigured lights,
beacon UUID) live in `src/config.h`, grouped into selectable `<mesh>` blocks.
Pick which mesh a build targets by setting `BRMESH_MESH` in `platformio.ini`
(`build_flags`):

```ini
build_flags = -D BRMESH_MESH=BRMESH_PAUL   # placeholder key 12345678 (default)
# or
build_flags = -D BRMESH_MESH=BRMESH_OLD    # placeholder key 12345678
```

To control a new mesh, add a new `#elif BRMESH_MESH == ...` block in `config.h`
with its key, fixtures, and broker.

Serial test commands (e.g. `allon`, `alloff`, `red`, `gb <0-100>`) are handled
in `loop()` in `src/main.cpp`.

## Acknowledgements

The great existing projects that this work is based off of:

 - [BRMesh_homeassistant by @millskyle](https://github.com/millskyle/BRMesh_homeassistant)
 - [brMeshMQTT by @ArcadeMachinist](https://github.com/ArcadeMachinist/brMeshMQTT)
 - BRmesh-esp32-mqtt by @dsclee1](https://github.com/dsclee1/BRmesh-esp32-mqtt)
