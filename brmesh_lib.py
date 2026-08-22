"""Shared BRMesh Fastcon decoding + mesh-structure tracking.

Reused by monitor.py (live) and decode_log.py (offline log re-decode).
"""
import json

# AD type 0xFF + manufacturer company 0xfff0 -> bytes FF F0 FF on the wire.
BRMESH_SIG = "FFF0FF"
# Fixed header XOR key (XOR'd into body[0..3]).
BRMESH_DEFAULT_KEY = bytes([0x5e, 0x36, 0x7b, 0xc4])
# Light type codes seen in 18-byte replies.
LIGHT_TYPES = {
    "39AE": "Smart",
    "A1A8": "RGBW",
    "A0A8": "RGB",
}

# Placeholder example mesh key 12345678 (default when no key is supplied).
OLD_KEY = bytes([0x12, 0x34, 0x56, 0x78])

# XOR base bytes for the decrypted 12-byte payload (placeholder key 12345678).
#
# The app whitens the 16-byte Fastcon body (4 header bytes + 12 data bytes) with
# a fixed, input-independent LFSR stream seeded by whitening_init(0x25).  The
# sniffer then XORs body[4..15] back with the mesh key ("decrypted") and leaves
# body[16..21] (the last 6 data bytes) untouched in the "tail".  Because both
# the whitening mask and the key-XOR are positional, every plaintext data byte
# lands at a fixed output position XOR'd with a key-dependent base (see
# `_xorbases`).  These constants are that base for the placeholder key:
#   decrypted p8  = data[2] ^ BRIGHT_BASE  (single-light state/brightness)
#   decrypted p9  = data[3] ^ BLUE_BASE    (single-light blue)
#   decrypted p10 = data[4] ^ RED_BASE     (single-light red / group brightness)
#   decrypted p11 = data[5] ^ GREEN_BASE   (single-light green / group blue)
#   decrypted p7  = data[1] ^ ID_BASE      (device/group id)
BRIGHT_BASE = 0x60
BLUE_BASE   = 0x87
RED_BASE    = 0x7F
GREEN_BASE  = 0xB0
ID_BASE     = 0x44
BRIGHT_MAX  = 127


def _xorbases(key):
    """Position-dependent XOR bases mapping the plaintext data bytes to the
    bytes the sniffer exposes (`decrypted` p6..p11 and the raw `tail` t0..t3).

    The app builds a 16-byte Fastcon body ``[header(4), data(12)]``, whitens the
    full rf payload (fixed LFSR mask, position-only), and advertises it.  The
    data bytes sit at body[4..15]; the sniffer XORs body[4..15] with the mesh
    key to make the 12 "decrypted" bytes, and keeps body[16..21] (data[6..11])
    as the raw tail.  So:

        decrypted[6+j] = data[j] ^ key[(4+j)&3] ^ mask[25+j] ^ key[(6+j)&3]
        tail[j]        = data[6+j] ^ key[(10+j)&3] ^ mask[31+j]

    with the fixed whitening masks mask[25..30] = 98 08 24 CB 3B FC and
    mask[31..36] = 71 A3 F4 55 68 CF.  Returns the per-position bases.
    """
    if key is None:
        key = OLD_KEY
    k0, k1, k2, k3 = key
    return {
        "fam": k0 ^ k2 ^ 0x98,   # data[0] -> decrypted p6  (command byte)
        "id":  k1 ^ k3 ^ 0x08,   # data[1] -> decrypted p7  (device/group id)
        "d2":  k0 ^ k2 ^ 0x24,   # data[2] -> decrypted p8
        "d3":  k1 ^ k3 ^ 0xCB,   # data[3] -> decrypted p9
        "d4":  k0 ^ k2 ^ 0x3B,   # data[4] -> decrypted p10
        "d5":  k1 ^ k3 ^ 0xFC,   # data[5] -> decrypted p11
        "t0":  k2 ^ 0x71,        # data[6] -> tail[0]
        "t1":  k3 ^ 0xA3,        # data[7] -> tail[1]
        "t2":  k0 ^ 0xF4,        # data[8] -> tail[2]
        "t3":  k1 ^ 0x55,        # data[9] -> tail[3]
    }


def hex_key(s):
    """Parse a mesh key: '12345678', '0x12,0x34,0x56,0x78', or 4 hex bytes."""
    s = s.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    s = s.replace("0x", "").replace(",", "").replace(" ", "")
    b = bytes.fromhex(s)
    if len(b) != 4:
        raise ValueError("mesh key must be exactly 4 bytes (e.g. 12345678)")
    return b


def parse_brmesh(hexdata, key=None):
    """Decode a BRMesh advertisement. Returns an info dict, or None if the
    packet isn't a BRMesh packet (no FF F0 FF signature)."""
    idx = hexdata.find(BRMESH_SIG)
    if idx == -1:
        return None
    body_hex = hexdata[idx + len(BRMESH_SIG):]
    if len(body_hex) % 2:
        body_hex = body_hex[:-1]
    body = bytes.fromhex(body_hex)
    info = {"body_len": len(body), "body_hex": body_hex.upper()}

    if len(body) >= 4:
        hdr = bytes(body[i] ^ BRMESH_DEFAULT_KEY[i] for i in range(4))
        info["header"] = hdr.hex().upper()
        # Fastcon header byte 0 packs: forward<<7 | i<<4 | i2
        b0 = hdr[0]
        info["forward"] = (b0 >> 7) & 1
        info["cmd"] = (b0 >> 4) & 7
        info["i2"] = b0 & 0xF
        info["sequence"] = hdr[1]
        info["safe_key"] = hdr[2]
        info["checksum"] = hdr[3]

    if len(body) >= 16:
        dec = bytearray(body)
        if key:
            for j in range(12):          # Fastcon: body[4+j] ^= key[j & 3]
                dec[4 + j] ^= key[j & 3]
        # Only bytes [4:16] are key-encrypted; the tail (16+) is raw.
        info["decrypted"] = bytes(dec[4:16]).hex().upper()
        info["tail"] = bytes(body[16:]).hex().upper()
        info["fields"] = decode_fields(info["decrypted"], info["tail"], key)

    if len(body) == 18:                  # 18-byte light REPLY only (from repo)
        t = body[12:14].hex().upper()
        info["type"] = LIGHT_TYPES.get(t, t)
        info["reply_key"] = body[14:18].hex().upper()
    return info


def _decode_brightness(byte, base=BRIGHT_BASE):
    """Decode a single-light brightness byte (family B4/E4).

    The app/bridge send data[2] RAW (bit7 = ON, low7 = level), and the decrypted
    byte is `(0x80|level) ^ base`, so recover level with `(byte & 0x7F) ^ base`.
    Returns (percent, level).
    """
    level = (byte & 0x7F) ^ base
    return round(level * 100 / BRIGHT_MAX), level


def _color_name(r, g, b):
    """Name an RGB triple: primaries + secondaries + white, else RGB(rr,gg,bb)."""
    if r == g == b == 0:
        return "WHITE (W)"
    if r == 0xFF and g == 0 and b == 0:
        return "RED"
    if r == 0 and g == 0xFF and b == 0:
        return "GREEN"
    if r == 0 and g == 0 and b == 0xFF:
        return "BLUE"
    if r == 0xFF and g == 0xFF and b == 0:
        return "YELLOW"
    if r == 0xFF and g == 0 and b == 0xFF:
        return "MAGENTA"
    if r == 0 and g == 0xFF and b == 0xFF:
        return "CYAN"
    if r == g == b and r != 0:
        return f"WHITE({r:02X})"
    return f"RGB({r:02X},{g:02X},{b:02X})"


def _add_state_brightness(f, byte, base=BRIGHT_BASE):
    """Add state (bit7 = ON) and brightness (XOR 0-127 -> %) to a field dict."""
    f["state"] = "ON" if byte & 0x80 else "OFF"
    pct, level = _decode_brightness(byte, base)
    f["brightness_level"] = level
    f["brightness"] = pct


def _add_group_state_brightness(f, p10, base):
    """Group brightness byte (plaintext data[4] = p10 ^ base).

    bit7 = explicit power-ON flag, low7 = brightness level 0..127:
      0x00          -> OFF
      0x80          -> ON (power-on, restore last brightness)
      0x01..0x7F    -> brightness level (implies ON)
    Used by both the group (D4/D5) and group-color (05) families.
    """
    raw = p10 ^ base              # plaintext data[4]
    level = raw & 0x7F
    f["state"] = "ON" if (raw & 0x80) or level else "OFF"
    f["brightness_level"] = level
    f["brightness"] = round(level * 100 / BRIGHT_MAX)


def _add_color(f, p, B):
    """Decode XOR-encoded single-light RGB (p9=blue, p10=red, p11=green)."""
    b = B["d3"] ^ p[9]
    r = B["d4"] ^ p[10]
    g = B["d5"] ^ p[11]
    f["rgb"] = f"R={r:02X} G={g:02X} B={b:02X}"
    # RGBW floodlight: all-zero RGB drives the white (W) channel
    f["color"] = _color_name(r, g, b)
    if r == g == b == 0:
        f["w"] = "FF"


def decode_fields(payload_hex, tail_hex=None, key=None):
    """Interpret the decrypted 12-byte payload (+ raw 8-byte tail) for the
    Fastcon command format.

    The app whitens the body, so the sniffer's "decrypted" bytes p6..p11 hold
    the first 6 plaintext data bytes (XOR a key-dependent base) and the raw
    tail holds the last 6 (data[6..11]).  The family is the PLAINTEXT command
    byte data[0] = p6 ^ fam_base (key-agnostic); the layout after it depends:

      brightness (22/2D), color (72/7D)  -> p7=device, p8=brightness(bit7=ON)
      group (43/42)                      -> p7=group, p10=data[4]^base
                                           (bit7=ON flag, low7=level 0..127)
      group-color (93)                   -> p7=group, p10=brightness(bit7=ON,
                                           low7=level 0..127), p11=blue,
                                           tail[0..3]=red/green/white (x2 slots)
      color2 (23), music (86/88)         -> other layout
    """
    try:
        p = bytes.fromhex(payload_hex)
    except ValueError:
        return {}
    if len(p) < 12:
        return {}
    tail = bytes.fromhex(tail_hex) if tail_hex else None
    B = _xorbases(key)
    # The PLAINTEXT command byte is data[0] = decrypted p6 ^ fam_base. It is
    # key-agnostic (0x22=brightness, 0x72=color, 0x43=group, 0x93=group-color,
    # 0x23=color2, 0x86/0x88=music), so families decode identically for any
    # mesh key (any 4-byte mesh key, e.g. 12345678).
    cmd = p[6] ^ B["fam"]
    CMDS = {
        0x72: "color", 0x7D: "color",
        0x22: "brightness", 0x2D: "brightness",
        0x43: "group", 0x42: "group",
        0x93: "group-color",
        0x23: "color2",
        0x86: "music", 0x88: "music",
    }
    COLOR = {0x72, 0x7D}                 # single-light color (RGB XOR-encoded)
    SINGLE = {0x22, 0x2D, 0x72, 0x7D}    # single-light brightness + color
    GROUP = {0x43, 0x42}                 # group/all on/off/brightness
    GROUP_COLOR = {0x93}                 # group color (RGBW)
    f = {"opcode": f"0x{p[0]:02X}",
         "family": CMDS.get(cmd, f"0x{cmd:02X}"),
         "payload": payload_hex}
    if cmd in SINGLE:
        # single-light: p7=device, p8=state/brightness (bit7 = ON). The app/bridge
        # send data[1] = device RAW, so decrypted p7 = device ^ ID_BASE; recover
        # the device id by XORing the base back in.
        f["device"] = p[7] ^ B["id"]
        _add_state_brightness(f, p[8], B["d2"])
        if cmd in COLOR:
            # color (RGBW): p9/p10/p11 = XOR RGB (blue/red/green)
            _add_color(f, p, B)
    elif cmd in GROUP:
        # group/all on/off + brightness: p7=group, p10=data[4]^base.
        # data[4]: bit7=ON flag, low7=level (0x00=OFF, 0x80=ON, 1..127=brightness).
        f["group"] = p[7] ^ B["id"]
        _add_group_state_brightness(f, p[10], B["d4"])
    elif cmd in GROUP_COLOR:
        # group color (0x05): p7=group, p10=data[4]^base (bit7=ON, low7=level),
        # p11=blue, and the red/green + two white bytes live in the raw tail
        # bytes 0..3. RGB channels are 0..255; white is a single channel (RGBW)
        # written to both white slots (0x7F = full).
        f["group"] = p[7] ^ B["id"]
        _add_group_state_brightness(f, p[10], B["d4"])
        if tail and len(tail) >= 4:
            blue = B["d5"] ^ p[11]
            red = B["t0"] ^ tail[0]
            green = B["t1"] ^ tail[1]
            white = B["t2"] ^ tail[2]
            white2 = B["t3"] ^ tail[3]
            f["channels"] = (f"B={blue:02X} R={red:02X} G={green:02X} "
                             f"W={white:02X} W2={white2:02X}")
            f["rgb"] = f"R={red:02X} G={green:02X} B={blue:02X}"
            if white or white2:
                # Single white channel (RGBW): the app writes the same value to
                # both slots. Keep both only if they ever differ.
                f["w"] = (f"{white:02X}" if white == white2
                          else f"{white:02X}/{white2:02X}")
                if red == green == blue == 0:
                    f["color"] = "WHITE (W)"
                else:
                    f["color"] = (f"RGBW({red:02X},{green:02X},{blue:02X},"
                                  f"W={white:02X})")
            elif red == green == blue == 0:
                f["color"] = "OFF"
            else:
                f["color"] = _color_name(red, green, blue)
        else:
            f["color"] = "(tail missing)"
    return f


class MeshTracker:
    """Accumulates the discovered BRMesh mesh structure into a JSON file keyed
    by the mesh key. Same key -> same file (updated/accumulated); a different
    key -> a different file (new structure). No prompt at startup."""
    def __init__(self, mesh_key, path):
        self.key = mesh_key.hex().upper()
        self.path = path
        self.data = {"mesh_key": self.key, "lights": {}, "groups_seen": [],
                     "command_count": 0}
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            if d.get("mesh_key") == self.key:
                self.data = d
        except Exception:
            pass  # new/missing file -> start fresh

    def update(self, fields, ts, mac):
        self.data["command_count"] = self.data.get("command_count", 0) + 1
        device = fields.get("device")
        if device is not None:
            light = self.data["lights"].setdefault(str(device),
                                                   {"device": device})
            if "first_seen" not in light:
                light["first_seen"] = ts
            light["last_seen"] = ts
            light["last_mac"] = mac
            fam = fields.get("opcode")
            if fam:
                seen = light.setdefault("opcodes", [])
                if fam not in seen:
                    seen.append(fam)
            if "state" in fields:
                light["last_state"] = fields["state"]
            if "brightness" in fields:
                light["last_brightness"] = fields["brightness"]
            if "brightness_level" in fields:
                light["last_brightness_level"] = fields["brightness_level"]
            if fields.get("family") == "color":
                if "rgb" in fields:
                    light["last_rgb"] = fields["rgb"]
                if "color" in fields:
                    light["last_color"] = fields["color"]
                rgb = fields.get("payload")
                if rgb:
                    colors = light.setdefault("colors_seen", [])
                    if rgb not in colors:
                        colors.append(rgb)
                    cdec = light.setdefault("colors", [])
                    entry = {"payload": rgb, "rgb": fields.get("rgb"),
                             "color": fields.get("color")}
                    if entry not in cdec:
                        cdec.append(entry)
        # Record group/all events (on/off/brightness/color) for recreating
        # all capabilities.
        if fields.get("family") in ("group", "group-color"):
            evt = {"time": ts, "mac": mac,
                   "state": fields.get("state"),
                   "brightness": fields.get("brightness"),
                   "payload": fields.get("payload", "")}
            if "group" in fields:
                evt["group"] = fields["group"]
            if "color" in fields:
                evt["color"] = fields["color"]
            if "rgb" in fields:
                evt["rgb"] = fields["rgb"]
            if "channels" in fields:
                evt["channels"] = fields["channels"]
            self.data.setdefault("group_events", []).append(evt)
            g = fields.get("payload", "")
            if g and g not in self.data["groups_seen"]:
                self.data["groups_seen"].append(g)
        self.data["updated"] = ts

    def save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass
