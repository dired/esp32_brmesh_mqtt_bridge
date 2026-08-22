# esp32_brmesh_mqtt_bridge

Control an **existing BRMesh LED floodlight mesh** from MQTT (Home Assistant),
without re-pairing the lights. Two ESP32 boards do the work — one passively
sniffs the BLE traffic, the other acts as the MQTT → BLE bridge. A Linux Tkinter
GUI (and a small CLI) let you switch, dim, and colour the lights cheaply — no
DMX/RGBW hardware needed.

## What's special about this

Earlier BRMesh bridge implementations had to **re-pair** every light (send a
fresh key-set handshake, which makes each fixture flash). This project joins an
**already-paired** installation instead:

1. Pull the existing mesh **key** off the phone app via `adb logcat`.
2. Discover the **device IDs** of the lights by sniffing (or from the same ADB log).
3. Put the key + device IDs in `esp32_mqtt_bridge/src/config.h`.
4. The bridge then speaks directly to the already-paired lights — no pairing pass.

Sniffing is therefore **necessary** to embed the bridge node in an existing
BRMesh setup: it tells you the key and the device IDs the mesh is already using.

## Intended use

Run this bridge **alongside an existing BRMesh installation**. You send MQTT
commands (per-light on/off/brightness/colour, or the "all lights" group), and
the bridge turns them into the same BLE advertising packets the app sends. A
handy GUI exists for Linux (`tools/brmesh_control_gui.py`).

> Roadmap: the original goal was to add **Art-Net** instead of MQTT for control.
> That can follow later — the MQTT + GUI path is a cheaper/faster way to make
> something happen with the lights than driving them through DMX/RGBW signals.

## What you need

- **One ESP32 for the bridge** (`esp32_mqtt_bridge/`) — receives MQTT commands
  and emits BRMesh BLE packets. It is configured with the mesh key (from ADB)
  and the device IDs.
- **One ESP32 for the sniffer** (`esp32_sniffer/`) — only needed if you want to
  populate `logs/` and discover the mesh. It passively captures BRMesh
  advertisements and forwards them to your laptop over WiFi/UDP.

Both are classic ESP32 or ESP32-S3 boards. The bridge additionally needs your
MQTT broker (e.g. mosquitto / Home Assistant).

## Repository layout

| Path | What it is |
|------|------------|
| `esp32_mqtt_bridge/` | The MQTT → BLE bridge firmware (PlatformIO) |
| `esp32_sniffer/` | The passive BLE sniffer firmware (PlatformIO, WiFi/UDP) |
| `monitor_remote.py` | Laptop-side UDP monitor + decoder (live sniffer view) |
| `monitor.py` | Shared monitor code + the legacy USB-serial entry point |
| `brmesh_lib.py` | Shared Fastcon decode + mesh-structure tracking |
| `decode_log.py` | Re-decode a raw sniff log with a key |
| `convert_capture.py` | Re-render a raw sniff log as a readable capture |
| `tools/brmesh_control_gui.py` | Linux GUI (MQTT control, groups, persistent state) |
| `tools/bridge_test_cmds.py` | Small CLI for firing MQTT test commands |
| `BRMESH_PROTOCOL.md` | Reverse-engineering notes (framing, encryption, decoding) |

## Getting your mesh key (ADB)

The bridge needs the 4-byte mesh key that your BRMesh app already uses.

- **WiFi debugging** (Android 11+) or **USB debugging** both work fine these
  days. For USB debugging, not all data-capable cables worked for me. For wifi-debugging, simply click in developer options to pair via code, it shows the ip port and 6-digit code and you simply run `adb pair {ip}:{port}` then you can use logcat immediately.
- With the app open, toggle a light and watch the log:

```bash
adb logcat | grep jyq
# look for a line like:
#   jyq_helper: getPayloadWithInnerRetry---> payload:...,  key: <8 hex chars>
```

The 8 hex characters after `key:` are your mesh key. Put them in
`esp32_mqtt_bridge/src/config.h` (see below).

## Getting the device IDs

Each light (and the "All" group) has an ID. Two ways to find them:

- **Sniffing** — run the sniffer + `monitor_remote.py` with the key and watch
  the decoded `device=` / `group=` fields. The IDs also accumulate in
  `logs/<KEY>/brmesh_mesh_<KEY>.json`.
- **ADB** — the same `adb logcat` capture that reveals the key also contains
  the plaintext commands, including the device IDs.

## Configuring the bridge

Edit `esp32_mqtt_bridge/src/config.h`:

- `MY_KEY_0..3` — your mesh key (the file ships with the placeholder `12345678`).
- `MQTT_BROKER_ADDR` — your MQTT broker IP.
- `WIFI_SSID` / `WIFI_PASS` — your WiFi.
- `preconfiguredLights[]` — one entry per light: device number, type, HA id, name.

In case you ever have multiple meshes, you can pick which brmesh (and according config-block) a build targets in `esp32_mqtt_bridge/platformio.ini`:

```ini
build_flags = -D BRMESH_MESH=BRMESH_PAUL
```

## Building & flashing (PlatformIO)

```bash
python3 -m pip install -U platformio

# bridge firmware
cd esp32_mqtt_bridge
pio run -e esp32dev          # or -e esp32s3
pio run -e esp32dev -t upload

# sniffer firmware
cd ../esp32_sniffer
pio run -e esp32dev -t upload
```

Set `upload_port` / `monitor_port` in each `platformio.ini` if `pio` can't find
the board.

## Sniffing (populating `logs/`)

Flash the sniffer, put the ESP32 in range of the mesh, then on your laptop:

```bash
python3 -m pip install -r requirements.txt
python3 monitor_remote.py --key 12345678
```

The sniffer announces itself via mDNS (`brmesh-sniffer.local`) and broadcasts
`BLE|...` lines over UDP 41234. `monitor_remote.py` renders them live and writes
`logs/<KEY>/brmesh_remote_*.log` plus the accumulated `brmesh_mesh_<KEY>.json`.

Keys while running: `d` toggle dedup, `n` tag last packet, `q` quit.

## Controlling the lights

Send MQTT commands (topics are published by the bridge via ArduinoHA
discovery), or use the GUI:

```bash
# Linux GUI (system python has tkinter; paho comes from the local .venv)
/usr/bin/python3 tools/brmesh_control_gui.py

# quick CLI test
python3 tools/bridge_test_cmds.py light 34 r
python3 tools/bridge_test_cmds.py group on
```

Before using the GUI or CLI, set the MQTT broker and the bridge's device id (lowercase hex of the bridge ESP32's
WiFi MAC) at
the top of each script (`CONFIG` in `brmesh_control_gui.py`, `BROKER`/`DEV` in
`bridge_test_cmds.py`).

## Based on

This builds on great existing projects:

- [BRMesh_homeassistant](https://github.com/millskyle/BRMesh_homeassistant) by @millskyle
- [brMeshMQTT](https://github.com/ArcadeMachinist/brMeshMQTT) by @ArcadeMachinist
- `BRMesh_Artnet_Bridge` — the Linux Art-Net bridge this protocol work started from

See `BRMESH_PROTOCOL.md` for the protocol details.
