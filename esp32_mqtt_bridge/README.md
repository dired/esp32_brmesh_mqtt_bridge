
# BRmesh esp32 mqtt

An ESP32 MQTT Home Assistant implementation of the BRmesh app to control lights.

Automatically adds lights and makes them available via an MQTT broker.


## Acknowledgements

The great existing projects that this work is based off of:

 - [BRMesh_homeassistant by @millskyle](https://github.com/millskyle/BRMesh_homeassistant)
 - [brMeshMQTT by @ArcadeMachinist](https://github.com/ArcadeMachinist/brMeshMQTT)

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

## Usage

Turn off your lights.

Turn on the ESP32, if using an ESP32 Dev Module (like I am) the blue light will come on to show it's in scanning mode.

Turn on your lights.

The ESP32 sends an "alive" message to the lights, receives a response back from them, sends a new key (which makes each light flash), and they respond back to say they're set. These are then made available as MQTT devices (should be viewable on your broker by using https://github.com/thomasnordquist/MQTT-Explorer).

You're good to go!




## Bugs

I'm unable to test the "ColorTemperature" code properly as it's not a function that my lights have.

Adding lights has occasionally been flakey. I've tested this code on a group of 7 lights, for which it worked fine, but you'll have to see how you get on. Some of the polling times for the BLE Advertising frames might need adjustment.

## Contributing

Contributions are always welcome!

