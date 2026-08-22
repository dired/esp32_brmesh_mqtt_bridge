#!/usr/bin/env python3
"""brmesh_control_gui.py - BRMesh floodlight control GUI (MQTT + persistent state).

Features
--------
- Loads the known lights from esp32_mqtt_bridge/src/config.h (preconfiguredLights[]).
- Per-light: ON/OFF, brightness, W/R/G/B + custom color, editable persistent NAME,
  and a color DOT showing the exact color the light is currently set to.
- Persistent state: every change is written to a JSON file (lamp state, continuously
  updated) so the GUI remembers the last state of ALL lamps across restarts.
- Settings (names, groups, window) in an INI file.
- "All On", "All Off", and "Recover" (re-apply the last remembered state) buttons.
- Groups: created from multi-select (drag / click / shift / ctrl) of lights.
  Each group has: name, member color dots, and effects:
    * Same color      - all lights in the group get one color
    * Rainbow         - hue spread evenly across the group's lights
    * Random color    - every N seconds, pick 1-4 random lights and shift their
                         color by a random amount (0..max degrees on the colorwheel)
    * Shadow chase    - an "off" spot wanders through the group (one goes off, the
                         next turns on, ...) for a configurable number of turns

Transport
---------
MQTT. Publishes per-light commands to the bridge. Firmware (esp32_mqtt_bridge) is
expected to subscribe and emit BRMesh. Topic base is configurable (default brmesh).
"""
import os
import re
import sys
import json
import time
import colorsys
import configparser
import threading
import random
import math
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
BRIDGE = os.path.normpath(os.path.join(ROOT, "esp32_mqtt_bridge"))
VENV_SITE = os.path.normpath(os.path.join(ROOT, ".venv", "lib", "python3.12", "site-packages"))
if VENV_SITE not in sys.path:
    sys.path.insert(0, VENV_SITE)

import tkinter as tk
from tkinter import ttk, colorchooser, messagebox, simpledialog
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG = {
    "broker": "YOUR_MQTT_BROKER",   # e.g. "192.168.1.10" or a hostname
    "port": 1883,
    # Bridge HA unique id = lowercase hex of the bridge's WiFi MAC (the ESP32
    # publishes it via Home Assistant discovery). Per-light topics use aha/ prefix.
    "dev_id": "YOUR_BRIDGE_ID",
    "state_file": os.path.join(ROOT, "brmesh_state.json"),
    "settings_file": os.path.join(ROOT, "brmesh_settings.ini"),
}

LIGHT_TYPES = {"Smart": (0x39, 0xAE), "RGBW": (0xA1, 0xA8), "RGB": (0xA0, 0xA8)}


def parse_config_lights():
    """Extract preconfiguredLights from the ACTIVE mesh block in esp32_mqtt_bridge/src/config.h.

    Only returns lights from the block whose #if/#elif BRMESH_MESH selector matches
    the active build (from platformio.ini), so we don't mix mesh light-sets.
    """
    # find active mesh from platformio.ini
    active = None
    try:
        with open(os.path.join(BRIDGE, "platformio.ini")) as f:
            for line in f:
                m = re.search(r"BRMESH_MESH=(BRMESH_\w+)", line)
                if m:
                    active = m.group(1)
    except OSError:
        pass

    lights = []
    path = os.path.join(BRIDGE, "src", "config.h")
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return lights

    # isolate the active mesh's #if/#elif block body
    block = None
    blocks = re.split(r"#(?:if|elif)\s+BRMESH_MESH\s*==\s*(BRMESH_\w+)", text)
    # blocks[0]=pre, then (sel, body) pairs
    for i in range(1, len(blocks) - 1, 2):
        if blocks[i] == active:
            block = blocks[i + 1]
            break
    if block is None:
        block = text  # fallback: whole file

    pat = re.compile(r"\{\s*(0x[0-9a-fA-F]+|\d+)\s*,\s*\{[^}]*\}\s*,\s*\"([^\"]+)\"\s*,\s*\"([^\"]+)\"\s*\}")
    for m in pat.finditer(block):
        num = int(m.group(1), 16) if m.group(1).lower().startswith("0x") else int(m.group(1))
        lights.append({"num": num, "id": m.group(2).lower(), "name": m.group(3)})
    return lights


def hsv_to_rgb(h, s, v):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360.0, s, v)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def rgb_to_hsv(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    return h * 360, s, v


def rgb_hex(r, g, b):
    return "#%02x%02x%02x" % (r, g, b)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
class Store:
    """INI (settings/names/groups) + JSON (live lamp state, continuously updated)."""

    def __init__(self):
        self.ini = configparser.ConfigParser()
        self.settings_file = CONFIG["settings_file"]
        self.state_file = CONFIG["state_file"]
        self.load_ini()
        self.state = self.load_state()

    def load_ini(self):
        if os.path.exists(self.settings_file):
            self.ini.read(self.settings_file)
        if not self.ini.has_section("names"):
            self.ini.add_section("names")
        if not self.ini.has_section("groups"):
            self.ini.add_section("groups")
        if not self.ini.has_section("window"):
            self.ini.add_section("window")

    def save_ini(self):
        try:
            with open(self.settings_file, "w") as f:
                self.ini.write(f)
        except OSError as e:
            print("ini save error:", e)

    def load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self):
        # continuously-updated lamp state file
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.state, f, indent=2)
            os.replace(tmp, self.state_file)
        except OSError as e:
            print("state save error:", e)

    def get_light_state(self, lid):
        st = self.state.setdefault(lid, {"on": True, "r": 255, "g": 255, "b": 255,
                                          "bri": 100, "name": ""})
        return st

    def set_light_state(self, lid, **kw):
        st = self.get_light_state(lid)
        st.update(kw)
        self.save_state()

    def update_live(self, lid, **kw):
        """Update in-memory state WITHOUT persisting (used by chases)."""
        st = self.get_light_state(lid)
        st.update(kw)

    def get_name(self, lid):
        return self.ini.get("names", lid, fallback="")

    def set_name(self, lid, name):
        self.ini.set("names", lid, name)
        self.save_ini()

    def get_group(self, gid):
        raw = self.ini.get("groups", gid, fallback="")
        return [x for x in raw.split(",") if x] if raw else []

    def set_group(self, gid, members):
        self.ini.set("groups", gid, ",".join(members))
        self.save_ini()

    def get_group_names(self):
        return self.ini.options("groups")

    # ---- scenes (persisted in the JSON state file) -----------------------
    def get_scenes(self):
        return self.state.setdefault("scenes", {})

    def save_scene(self, name, lights_state):
        self.get_scenes()[name] = lights_state
        self.save_state()

    def get_scene(self, name):
        return self.get_scenes().get(name, {})


# ---------------------------------------------------------------------------
# MQTT transport
# ---------------------------------------------------------------------------
class MqttControl:
    def __init__(self):
        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.connected = False

    def _on_connect(self, c, u, f, rc):
        self.connected = (rc == 0)
        print("MQTT connected:", rc)

    def _on_disconnect(self, c, u, rc):
        self.connected = False
        print("MQTT disconnected:", rc)

    def start(self):
        try:
            self.client.connect(CONFIG["broker"], CONFIG["port"], keepalive=30)
            self.client.loop_start()
        except Exception as e:
            print("MQTT connect error:", e)

    def stop(self):
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def _topic(self, lid, sub):
        # Home-Assistant ArduinoHA topic layout for the bridge:
        #   aha/{dev}/{id}/rgb_cmd_t  (r,g,b)
        #   aha/{dev}/{id}/bri_cmd_t  (0..127)
        #   aha/{dev}/{id}/cmd_t      (ON/OFF)
        return "aha/%s/%s/%s" % (CONFIG["dev_id"], lid, sub)

    def light_cmd(self, lid, on):
        self.client.publish(self._topic(lid, "cmd_t"), "ON" if on else "OFF")

    def light_rgb(self, lid, r, g, b):
        self.client.publish(self._topic(lid, "rgb_cmd_t"), "%d,%d,%d" % (r, g, b))

    def light_bri(self, lid, pct):
        # bridge light brightness scale is 127 (bri_scl from discovery)
        self.client.publish(self._topic(lid, "bri_cmd_t"), str(round(int(pct) * 127 / 100)))


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.store = Store()
        self.mqtt = MqttControl()
        self.lights = parse_config_lights()  # [{num,id,name}]
        self.by_id = {l["id"]: l for l in self.lights}
        self.group_widgets = {}
        self.scene_var = tk.StringVar()
        self._build()
        self.mqtt.start()
        self._tick_random()

    # ---- window close ----------------------------------------------------
    def on_close(self):
        self.mqtt.stop()
        self.store.save_state()
        self.store.save_ini()
        self.root.destroy()

    # ---- helpers ---------------------------------------------------------
    def label_for(self, lid):
        st = self.store.get_light_state(lid)
        return st.get("name") or self.by_id[lid]["name"] if lid in self.by_id else lid

    def _send_state(self, lid):
        """Publish the light's current stored state to MQTT (used by Recover)."""
        st = self.store.get_light_state(lid)
        if st["on"]:
            self.mqtt.light_rgb(lid, st["r"], st["g"], st["b"])
            self.mqtt.light_bri(lid, st["bri"])
            self.mqtt.light_cmd(lid, True)
        else:
            self.mqtt.light_cmd(lid, False)

    # ---- build UI --------------------------------------------------------
    def _build(self):
        self.root.title("BRMesh Control")
        self.root.geometry("1100x700")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        toolbar = ttk.Frame(self.root, padding=6)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="ALL ON", command=self.all_on).pack(side="left", padx=4)
        ttk.Button(toolbar, text="ALL OFF", command=self.all_off).pack(side="left", padx=4)
        ttk.Button(toolbar, text="RECOVER", command=self.recover).pack(side="left", padx=4)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)

        # scenes
        ttk.Label(toolbar, text="Scene:").pack(side="left")
        ttk.Button(toolbar, text="Save", command=self.scene_save).pack(side="left", padx=2)
        self.scene_combo = ttk.Combobox(toolbar, textvariable=self.scene_var, width=14)
        self.scene_combo.pack(side="left", padx=2)
        self._refresh_scene_combo()
        ttk.Button(toolbar, text="Load", command=self.scene_load).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Update", command=self.scene_update).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)

        ttk.Label(toolbar, text="broker=%s" % CONFIG["broker"]).pack(side="left")

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # Left: lights list (with multiselect)
        left = ttk.Frame(paned, padding=6)
        paned.add(left, weight=3)
        ttk.Label(left, text="Lights  (click / shift / ctrl / drag to select)").pack(anchor="w")
        self.listbox = tk.Listbox(left, selectmode="extended", exportselection=False, height=20)
        self.listbox.pack(fill="both", expand=True, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        self.listbox.bind("<Double-Button-1>", lambda e: self._make_group_from_selection())
        ttk.Button(left, text="+ New Group from selection", command=self._make_group_from_selection).pack(pady=2)
        self._redraw_listbox()

        # Right: details + groups
        right = ttk.Frame(paned, padding=6)
        paned.add(right, weight=2)
        self.detail = ttk.LabelFrame(right, text="Selected light detail")
        self.detail.pack(fill="x", pady=4)
        self._build_detail_empty()

        self.groups_frame = ttk.LabelFrame(right, text="Groups")
        self.groups_frame.pack(fill="both", expand=True, pady=4)
        self._redraw_groups()

    # ---- lights list -----------------------------------------------------
    def _redraw_listbox(self):
        self.listbox.delete(0, "end")
        for l in self.lights:
            st = self.store.get_light_state(l["id"])
            name = st.get("name") or l["name"]
            dot = rgb_hex(st["r"], st["g"], st["b"])
            on = "ON" if st["on"] else "OFF"
            self.listbox.insert("end", "%s  [%s]  %s  %s" % (l["id"], on, name, dot))

    def _selected_ids(self):
        sel = [self.listbox.get(i) for i in self.listbox.curselection()]
        ids = []
        for line in sel:
            m = re.match(r"([0-9a-fA-F]+)\s+\[", line)
            if m:
                ids.append(m.group(1))
        return ids

    # ---- detail ----------------------------------------------------------
    def _build_detail_empty(self):
        for w in self.detail.winfo_children():
            w.destroy()
        ttk.Label(self.detail, text="Select one or more lights in the list.").pack(padx=6, pady=4)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

    def _on_select(self, _e=None):
        sel = self._selected_ids()
        if not sel:
            return
        if len(sel) == 1:
            self._build_detail_single(sel[0])
        else:
            self._build_detail_multi(sel)

    def _build_detail_single(self, lid):
        for w in self.detail.winfo_children():
            w.destroy()
        st = self.store.get_light_state(lid)
        l = self.by_id.get(lid, {})
        name = st.get("name") or l.get("name", lid)

        row = ttk.Frame(self.detail)
        row.pack(fill="x", padx=6, pady=4)
        # color dot
        dot = tk.Canvas(row, width=24, height=24, highlightthickness=1)
        dot.pack(side="left", padx=4)
        self._paint_dot(dot, st["r"], st["g"], st["b"])

        ttk.Label(row, text="ID %s" % lid, width=10).pack(side="left")
        namevar = tk.StringVar(value=name)
        ent = ttk.Entry(row, textvariable=namevar, width=12)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda e: self._save_name(lid, namevar.get()))

        ttk.Button(row, text="ON", command=lambda: self._set_on(lid, True)).pack(side="left", padx=2)
        ttk.Button(row, text="OFF", command=lambda: self._set_on(lid, False)).pack(side="left", padx=2)
        ttk.Button(row, text="Pick Color", command=lambda: self._pick_color(lid)).pack(side="left", padx=4)

        row2 = ttk.Frame(self.detail)
        row2.pack(fill="x", padx=6, pady=4)
        for key, (r, g, b) in {"W": (0, 0, 0), "R": (255, 0, 0), "G": (0, 255, 0), "B": (0, 0, 255)}.items():
            ttk.Button(row2, text=key, width=3,
                       command=lambda kk=key, rr=r, gg=g, bb=b: self._set_color(lid, rr, gg, bb)).pack(side="left", padx=2)

        row3 = ttk.Frame(self.detail)
        row3.pack(fill="x", padx=6, pady=4)
        ttk.Label(row3, text="Bri").pack(side="left")
        bvar = tk.IntVar(value=st["bri"])
        ttk.Scale(row3, from_=0, to=100, variable=bvar, orient="horizontal",
                  command=lambda v: self._set_bri(lid, v)).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(row3, textvariable=bvar).pack(side="left")

    def _build_detail_multi(self, ids):
        for w in self.detail.winfo_children():
            w.destroy()
        ttk.Label(self.detail, text="%d lights selected: %s" % (len(ids), ",".join(ids))).pack(anchor="w", padx=6, pady=4)
        row = ttk.Frame(self.detail)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Button(row, text="All ON", command=lambda: self._set_many_on(ids, True)).pack(side="left", padx=2)
        ttk.Button(row, text="All OFF", command=lambda: self._set_many_on(ids, False)).pack(side="left", padx=2)
        ttk.Button(row, text="Same color", command=lambda: self._make_group("group", ids)).pack(side="left", padx=4)

    # ---- light actions ---------------------------------------------------
    def _paint_dot(self, canvas, r, g, b):
        canvas.delete("all")
        canvas.create_oval(2, 2, 22, 22, fill=rgb_hex(r, g, b), outline="black")

    def _save_name(self, lid, name):
        self.store.set_name(lid, name)
        self.store.set_light_state(lid, name=name)
        self._redraw_listbox()

    def _set_on(self, lid, on):
        self.store.set_light_state(lid, on=on)
        self.mqtt.light_cmd(lid, on)
        self._redraw_listbox()
        self._on_select()

    def _set_many_on(self, ids, on):
        for lid in ids:
            self.store.set_light_state(lid, on=on)
            self.mqtt.light_cmd(lid, on)
        self._redraw_listbox()

    def _set_color(self, lid, r, g, b, on=True):
        self.store.set_light_state(lid, r=r, g=g, b=b, on=on)
        self.mqtt.light_rgb(lid, r, g, b)
        self.mqtt.light_cmd(lid, on)
        self._redraw_listbox()
        self._refresh_group_dots()
        self._on_select()

    def _pick_color(self, lid):
        st = self.store.get_light_state(lid)
        rgb = colorchooser.askcolor(color=rgb_hex(st["r"], st["g"], st["b"]),
                                    parent=self.root, title="Color for %s" % lid)
        if rgb and rgb[0]:
            r, g, b = (int(x) for x in rgb[0])
            self._set_color(lid, r, g, b)

    def _set_bri(self, lid, pct):
        self.store.set_light_state(lid, bri=int(float(pct)))
        self.mqtt.light_bri(lid, int(float(pct)))

    # ---- global actions --------------------------------------------------
    def all_on(self):
        for l in self.lights:
            self._send_state(l["id"])
        self._redraw_listbox()

    def all_off(self):
        for l in self.lights:
            self.store.set_light_state(l["id"], on=False)
            self.mqtt.light_cmd(l["id"], False)
        self._redraw_listbox()

    def recover(self):
        """Re-apply the last remembered state of all lamps (from memory/state file)."""
        for l in self.lights:
            self._send_state(l["id"])
        self._redraw_listbox()

    # ---- scenes ----------------------------------------------------------
    def _refresh_scene_combo(self):
        names = sorted(self.store.get_scenes().keys())
        self.scene_combo["values"] = names

    def _snapshot_all(self):
        snap = {}
        for l in self.lights:
            st = self.store.get_light_state(l["id"])
            snap[l["id"]] = {"r": st["r"], "g": st["g"], "b": st["b"],
                             "on": st["on"], "bri": st["bri"]}
        return snap

    def scene_save(self):
        name = simpledialog.askstring("Save scene", "Scene name:", parent=self.root)
        if not name:
            return
        self.store.save_scene(name, self._snapshot_all())
        self._refresh_scene_combo()
        self.scene_var.set(name)

    def scene_update(self):
        name = self.scene_var.get().strip()
        if not name:
            messagebox.showinfo("Scene", "Select a scene in the dropdown to update it.")
            return
        self.store.save_scene(name, self._snapshot_all())
        self._refresh_scene_combo()

    def scene_load(self):
        name = self.scene_var.get().strip()
        scene = self.store.get_scene(name)
        if not scene:
            messagebox.showinfo("Scene", "Select a scene to load.")
            return
        for lid, s in scene.items():
            if lid not in self.by_id:
                continue
            self.store.set_light_state(lid, r=s.get("r", 255), g=s.get("g", 255),
                                       b=s.get("b", 255), on=s.get("on", True),
                                       bri=s.get("bri", 100))
            if s.get("on", True):
                self.mqtt.light_rgb(lid, s.get("r", 255), s.get("g", 255), s.get("b", 255))
                self.mqtt.light_bri(lid, s.get("bri", 100))
                self.mqtt.light_cmd(lid, True)
            else:
                self.mqtt.light_cmd(lid, False)
        self._redraw_listbox()

    # ---- groups ----------------------------------------------------------
    def _make_group_from_selection(self):
        ids = self._selected_ids()
        if not ids:
            messagebox.showinfo("No selection", "Select lights first (click/shift/ctrl/drag).")
            return
        self._make_group("group", ids)

    def _make_group(self, gid, members):
        # unique gid
        base = gid
        i = 1
        while self.store.get_group(base):
            base = "%s%d" % (gid, i)
            i += 1
        self.store.set_group(base, members)
        self._redraw_groups()

    def _redraw_groups(self):
        for w in self.groups_frame.winfo_children():
            w.destroy()
        gnames = self.store.get_group_names()
        if not gnames:
            ttk.Label(self.groups_frame, text="No groups yet. Select lights -> New Group.").pack(padx=6, pady=4)
            return
        canvas = tk.Canvas(self.groups_frame)
        vsb = ttk.Scrollbar(self.groups_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.group_widgets = {}
        for gid in gnames:
            self._group_widget(inner, gid)

    def _refresh_group_dots(self):
        """Repaint every group's member dots from the current stored colors."""
        for gid, w in self.group_widgets.items():
            for lid, c in w.get("dots", {}).items():
                st = self.store.get_light_state(lid)
                c.delete("all")
                self._paint_dot(c, st["r"], st["g"], st["b"])

    def _group_widget(self, parent, gid):
        members = self.store.get_group(gid)
        box = ttk.LabelFrame(parent, text=gid)
        box.pack(fill="x", padx=4, pady=4)
        w = {
            "members": members,
            "mode": "shadow",       # "shadow" | "random"
            "state": "stopped",     # stopped | running | paused | idle
            "pre_colors": None,
            "pos": 0.0,
            "job": None,
            "dots": {},             # lid -> member-dot canvas
        }
        self.group_widgets[gid] = w

        # header
        head = ttk.Frame(box)
        head.pack(fill="x", padx=4, pady=2)
        namevar = tk.StringVar(value=gid)
        ent = ttk.Entry(head, textvariable=namevar, width=12)
        ent.pack(side="left", padx=2)
        ent.bind("<Return>", lambda e: self._rename_group(gid, namevar.get()))
        ttk.Button(head, text="Same", command=lambda: self._group_same(gid)).pack(side="left", padx=2)
        ttk.Button(head, text="Rainbow", command=lambda: self._group_rainbow(gid)).pack(side="left", padx=2)
        ttk.Button(head, text="Del", command=lambda: self._del_group(gid)).pack(side="left", padx=4)

        # member dots (keep canvas refs so we can repaint on color change)
        dots = ttk.Frame(box)
        dots.pack(fill="x", padx=4, pady=2)
        for lid in members:
            st = self.store.get_light_state(lid)
            c = tk.Canvas(dots, width=18, height=18, highlightthickness=1)
            c.pack(side="left", padx=2)
            self._paint_dot(c, st["r"], st["g"], st["b"])
            w["dots"][lid] = c

        # mode selector
        mode_row = ttk.Frame(box)
        mode_row.pack(fill="x", padx=4, pady=2)
        ttk.Label(mode_row, text="Mode:").pack(side="left")
        modevar = tk.StringVar(value="shadow")
        ttk.Radiobutton(mode_row, text="Shadow", value="shadow", variable=modevar,
                        command=lambda: self._set_mode(gid, "shadow")).pack(side="left", padx=2)
        ttk.Radiobutton(mode_row, text="Random", value="random", variable=modevar,
                        command=lambda: self._set_mode(gid, "random")).pack(side="left", padx=2)

        # periods: optional gap between passes (0 = continuous) + run length + random max deg
        per = ttk.Frame(box)
        per.pack(fill="x", padx=4, pady=2)
        ttk.Label(per, text="gap between passes(s):").pack(side="left")
        ivar = tk.DoubleVar(value=0.0)     # default 0 = run continuously
        ttk.Entry(per, textvariable=ivar, width=6).pack(side="left", padx=2)
        ttk.Label(per, text="run length(s):").pack(side="left", padx=(8, 2))
        dvar = tk.DoubleVar(value=10.0)    # default 10 s
        ttk.Entry(per, textvariable=dvar, width=6).pack(side="left", padx=2)
        ttk.Label(per, text="max\u00b0:").pack(side="left", padx=(8, 2))
        avar = tk.DoubleVar(value=120.0)
        ttk.Entry(per, textvariable=avar, width=5).pack(side="left", padx=2)
        w["vars"] = {"interval": ivar, "duration": dvar, "maxdeg": avar}

        # transport: position circle (+ time readout) + play/pause/stop
        trans = ttk.Frame(box)
        trans.pack(fill="x", padx=4, pady=2)
        pos_frame = ttk.Frame(trans)
        pos_frame.pack(side="left", padx=4)
        pos_canvas = tk.Canvas(pos_frame, width=44, height=44, highlightthickness=1)
        pos_canvas.pack()
        w["pos_canvas"] = pos_canvas
        w["time_lbl"] = ttk.Label(pos_frame, text="0.0s / --s", width=14, anchor="center")
        w["time_lbl"].pack(pady=(2, 0))
        self._draw_pos(w, 0.0)
        ttk.Button(trans, text="\u25b6 Play", command=lambda: self._chase_play(gid)).pack(side="left", padx=2)
        ttk.Button(trans, text="\u23f8 Pause", command=lambda: self._chase_pause(gid)).pack(side="left", padx=2)
        ttk.Button(trans, text="\u23f9 Stop", command=lambda: self._chase_stop(gid)).pack(side="left", padx=2)
        w["status"] = ttk.Label(trans, text="stopped")
        w["status"].pack(side="left", padx=8)

    def _draw_pos(self, w, pos):
        c = w["pos_canvas"]
        c.delete("all")
        cx = cy = 22
        r = 18
        c.create_oval(cx - r, cy - r, cx + r, cy + r, outline="black", width=2)
        ang = pos * 2 * math.pi - math.pi / 2
        ex = cx + r * math.cos(ang)
        ey = cy + r * math.sin(ang)
        c.create_line(cx, cy, ex, ey, fill="red", width=2)

    def _set_mode(self, gid, mode):
        w = self.group_widgets.get(gid)
        if w:
            self._chase_stop(gid)
            w["mode"] = mode

    def _rename_group(self, gid, newname):
        if newname == gid:
            return
        members = self.store.get_group(gid)
        self.store.ini.remove_option("groups", gid)
        self.store.set_group(newname, members)
        self._redraw_groups()

    def _del_group(self, gid):
        self._chase_stop(gid)
        self.store.ini.remove_option("groups", gid)
        self.store.save_ini()
        self._redraw_groups()

    # ---- group effects ---------------------------------------------------
    def _group_same(self, gid):
        members = self.store.get_group(gid)
        if not members:
            return
        st = self.store.get_light_state(members[0])
        for lid in members:
            self._set_color(lid, st["r"], st["g"], st["b"])

    def _group_rainbow(self, gid):
        members = self.store.get_group(gid)
        n = len(members)
        if not n:
            return
        for i, lid in enumerate(members):
            h = i * 360.0 / n
            r, g, b = hsv_to_rgb(h, 1.0, 1.0)
            self._set_color(lid, r, g, b)

    # ---- chase engine ----------------------------------------------------
    def _live_color(self, lid, r, g, b, on=True):
        """Send color to MQTT + update in-memory display WITHOUT persisting."""
        self.store.update_live(lid, r=r, g=g, b=b, on=on)
        self.mqtt.light_rgb(lid, r, g, b)
        self.mqtt.light_cmd(lid, on)

    def _run_total(self, w, n):
        """Total run time (s) for the wheel sweep. Shadow accounts for the 3s min per turn."""
        duration = max(0.1, w["vars"]["duration"].get())
        if w["mode"] == "shadow":
            step_s = max(3.0, duration / max(1, n))
            return step_s * n
        return duration

    def _update_time_label(self, w, elapsed, total):
        lbl = w.get("time_lbl")
        if lbl:
            lbl.config(text="%.1fs / %ds" % (elapsed, int(total)))

    def _chase_play(self, gid):
        w = self.group_widgets.get(gid)
        if not w:
            return
        # snapshot pre-chase colors (only when (re)starting from stopped)
        if w["state"] in ("stopped", "idle"):
            w["pre_colors"] = {lid: dict(self.store.get_light_state(lid)) for lid in w["members"]}
            w["pos"] = 0.0
            w["elapsed"] = 0.0
            w["step_accum"] = 0.0
            w["shadow_idx"] = 0
            w["run_total"] = self._run_total(w, len(w["members"]))
        # if resuming from pause, keep elapsed/step accumulators
        w["state"] = "running"
        w["_last_tick_t"] = time.time()
        w["status"].config(text="running")
        self._update_time_label(w, w.get("elapsed", 0.0), w.get("run_total", 0.0))
        self._chase_tick(gid)

    def _chase_pause(self, gid):
        w = self.group_widgets.get(gid)
        if not w or w["state"] != "running":
            return
        w["state"] = "paused"
        if w.get("job"):
            self.root.after_cancel(w["job"])
            w["job"] = None
        w["status"].config(text="paused")

    def _chase_stop(self, gid):
        w = self.group_widgets.get(gid)
        if not w:
            return
        if w.get("job"):
            self.root.after_cancel(w["job"])
            w["job"] = None
        w["state"] = "stopped"
        w["pos"] = 0.0
        self._draw_pos(w, 0.0)
        self._update_time_label(w, 0.0, 0.0)
        w["status"].config(text="stopped")
        self._restore_pre(gid)

    def _restore_pre(self, gid):
        w = self.group_widgets.get(gid)
        if not w or not w.get("pre_colors"):
            return
        for lid, pc in w["pre_colors"].items():
            self._set_color(lid, pc.get("r", 255), pc.get("g", 255), pc.get("b", 255),
                            on=pc.get("on", True))
        w["pre_colors"] = None

    def _chase_tick(self, gid):
        w = self.group_widgets.get(gid)
        if not w or w["state"] != "running":
            return
        members = w["members"]
        n = len(members)
        if not n:
            w["state"] = "stopped"
            return
        duration = max(0.1, w["vars"]["duration"].get())
        interval = max(0.0, w["vars"]["interval"].get())

        # smooth time accumulator (pausing naturally freezes elapsed)
        now = time.time()
        prev = w.get("_last_tick_t", now)
        dt = min(now - prev, 0.5)   # clamp jumps after pause/resume
        w["_last_tick_t"] = now
        w["elapsed"] = w.get("elapsed", 0.0) + dt
        w["step_accum"] = w.get("step_accum", 0.0) + dt

        if w["mode"] == "shadow":
            step_s = max(3.0, duration / max(1, n))
            total = step_s * n
            w["run_total"] = total
            while w["step_accum"] >= step_s and w["elapsed"] <= total:
                self._shadow_step(gid)
                w["step_accum"] -= step_s
        else:  # random - no waiting, color shift every 0.5s
            total = duration
            w["run_total"] = total
            if w["step_accum"] >= 0.5:
                self._random_step(gid)
                w["step_accum"] = 0.0

        pos = min(1.0, w["elapsed"] / total) if total > 0 else 1.0
        w["pos"] = pos
        self._draw_pos(w, pos)
        self._update_time_label(w, min(w["elapsed"], total), total)

        if w["elapsed"] >= total:
            self._finish_run(gid, interval)
        else:
            w["job"] = self.root.after(100, lambda: self._chase_tick(gid))

    def _finish_run(self, gid, interval):
        w = self.group_widgets.get(gid)
        if not w:
            return
        w["pos"] = 0.0
        self._draw_pos(w, 0.0)
        self._update_time_label(w, 0.0, 0.0)
        # keep the chase running continuously - never stop by itself.
        # an optional gap between passes just delays the next pass while running.
        if interval > 0:
            w["status"].config(text="running - next pass in %ds" % int(interval))
            w["job"] = self.root.after(int(interval * 1000), lambda: self._chase_run_again(gid))
        else:
            w["status"].config(text="running")
            self._chase_run_again(gid)

    def _chase_run_again(self, gid):
        w = self.group_widgets.get(gid)
        if not w:
            return
        w["pos"] = 0.0
        w["elapsed"] = 0.0
        w["step_accum"] = 0.0
        w["shadow_idx"] = 0
        w["state"] = "running"
        w["status"].config(text="running")
        self._chase_tick(gid)

    def _shadow_step(self, gid):
        w = self.group_widgets.get(gid)
        members = w["members"]
        n = len(members)
        idx = w.get("shadow_idx", 0) % n
        # turn ONLY idx off, all others on with their base color (live, no persist)
        for i, lid in enumerate(members):
            base = (w["pre_colors"] or {}).get(lid, self.store.get_light_state(lid))
            on = (i != idx)
            self._live_color(lid, base.get("r", 255), base.get("g", 255), base.get("b", 255), on=on)
        self._refresh_group_dots()
        self._redraw_listbox()
        w["shadow_idx"] = w.get("shadow_idx", 0) + 1

    def _random_step(self, gid):
        w = self.group_widgets.get(gid)
        members = w["members"]
        n = len(members)
        maxdeg = w["vars"]["maxdeg"].get()
        k = random.randint(1, min(4, n))
        for lid in random.sample(members, k):
            st = self.store.get_light_state(lid)
            h, s, v = rgb_to_hsv(st["r"], st["g"], st["b"])
            dh = random.uniform(-maxdeg, maxdeg)
            r, g, b = hsv_to_rgb(h + dh, s, v)
            self._live_color(lid, r, g, b)
        self._refresh_group_dots()
        self._redraw_listbox()

    # ---- periodic --------------------------------------------------------
    def _tick_random(self):
        self.root.after(250, self._tick_random)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
