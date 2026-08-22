#!/usr/bin/env python3
"""Fire BRMesh bridge test commands over MQTT.

Validated topics for the BRMesh ESP32-S3 bridge (set BROKER / DEV below).
Data prefix is `aha` (ArduinoHA).

Usage:
  python3 bridge_test_cmds.py light <obj_id> w|r|g|b
  python3 bridge_test_cmds.py light <obj_id> <r>,<g>,<b>
  python3 bridge_test_cmds.py group on|off|<0-100>
  python3 bridge_test_cmds.py groupcolor w|r|g|b | <r>,<g>,<b>
  python3 bridge_test_cmds.py all          # single + group W,R,G,B

Each color is ONE MQTT publish -> ONE BLE packet, like the app (brightness is
embedded in the color command; single-light defaults to 100%).
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_SITE = os.path.normpath(os.path.join(HERE, "..", ".venv", "lib",
                                          "python3.12", "site-packages"))
if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)
import paho.mqtt.client as mqtt

BROKER = "YOUR_MQTT_BROKER"   # e.g. "192.168.1.10" or a hostname
DEV = "YOUR_BRIDGE_ID"        # lowercase hex of the bridge's WiFi MAC

# example HA light object id -> (name, device number); replace with your lights
LIGHTS = {
    "e242": ("Light_1", 1), "564e": ("Light_2", 2), "4667": ("Light_3", 3),
    "7b5d": ("Light_4", 4), "3c7d": ("Light_5", 5), "ff59": ("Light_6", 6),
}
RGB = {"w": (0, 0, 0), "r": (255, 0, 0), "g": (0, 255, 0), "b": (0, 0, 255)}


def publish(client, topic, payload):
    client.publish(topic, payload, qos=0)
    print(f"  -> {topic} = {payload}")


def parse_color(arg):
    a = arg.lower()
    return RGB[a] if a in RGB else tuple(int(x) for x in a.split(","))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1

    client = mqtt.Client()
    client.connect(BROKER, 1883, keepalive=30)
    client.loop_start()
    time.sleep(0.3)

    cmd = sys.argv[1]

    if cmd == "light":
        obj = sys.argv[2]
        if obj not in LIGHTS:
            print(f"unknown light id {obj}; known: {list(LIGHTS)}")
            return 1
        rgb = parse_color(sys.argv[3])
        publish(client, f"aha/{DEV}/{obj}/rgb_cmd_t", f"{rgb[0]},{rgb[1]},{rgb[2]}")

    elif cmd == "group":
        arg = sys.argv[2].lower()
        if arg in ("on", "off"):
            publish(client, f"aha/{DEV}/all_lights/cmd_t", "ON" if arg == "on" else "OFF")
        else:
            publish(client, f"aha/{DEV}/all_lights_brightness/cmd_t", arg)

    elif cmd == "groupcolor":
        rgb = parse_color(sys.argv[2])
        publish(client, f"aha/{DEV}/all_lights_color/rgb_cmd_t", f"{rgb[0]},{rgb[1]},{rgb[2]}")

    elif cmd == "all":
        obj = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in LIGHTS else "e242"
        for name, rgb in [("W", (0, 0, 0)), ("R", (255, 0, 0)),
                          ("G", (0, 255, 0)), ("B", (0, 0, 255))]:
            print(f"[single {LIGHTS[obj][0]} {name}]")
            publish(client, f"aha/{DEV}/{obj}/rgb_cmd_t", f"{rgb[0]},{rgb[1]},{rgb[2]}")
            time.sleep(1.5)
        for name, rgb in [("W", (0, 0, 0)), ("R", (255, 0, 0)),
                          ("G", (0, 255, 0)), ("B", (0, 0, 255))]:
            print(f"[group All {name}]")
            publish(client, f"aha/{DEV}/all_lights_color/rgb_cmd_t", f"{rgb[0]},{rgb[1]},{rgb[2]}")
            time.sleep(1.5)

    else:
        print(__doc__)
        return 1

    time.sleep(1.0)
    client.disconnect()
    client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
