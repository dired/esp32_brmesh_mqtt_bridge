#!/usr/bin/env python3
"""monitor.py — live BRMesh BLE-to-UART packet monitor.

Reads the machine-parseable `BLE|...` lines produced by ble_bridge.ino on the
ESP32 and renders them as a live, colored terminal table. Every raw line is
logged verbatim to a timestamped file.

Usage:
    python3 monitor.py [--port /dev/ttyUSB0] [--filter-oui] [--key 12345678]
    python3 monitor.py                        # auto-detect serial port

--key  decrypt BRMesh payloads with a 4-byte mesh key (8 hex chars)
       (extract it from the brMesh app via `adb logcat | grep jyq`)

Keys:
    d  toggle dedup mode (only show packets whose DATA changed for that MAC)
    q  quit
"""

import argparse
import csv
import datetime
import os
import re
import serial
import serial.tools.list_ports
import sys
import threading
import time

from brmesh_lib import hex_key, parse_brmesh, MeshTracker

try:
    from colorama import init, Fore, Style
    init()
except ImportError:
    # Minimal fallback if colorama isn't installed.
    class _F:
        def __getattr__(self, _): return ""
    Fore = _F()
    Style = _F()

BAUD = 115200

# BLE|MAC=<mac>|RSSI=<rssi>|LEN=<len>|DATA=<hex>|UPTIME=<ms>
LINE_RE = re.compile(
    r"^(?:BLE|UNK)\|MAC=([0-9a-f:]+)\|RSSI=(-?\d+)\|LEN=(\d+)\|DATA=([0-9A-F]*)\|UPTIME=(\d+)$"
)

# Bright 256-color palette for per-device coloring (fallback to ANSI if needed).
PALETTE = [f"\033[38;5;{c}m" for c in (208, 39, 46, 213, 51, 226, 162, 118, 209, 75,
                                       202, 81, 45, 220, 205, 121)]


def clr(color):
    return lambda s: f"{color}{s}{Fore.RESET}"


def rgb(rssi):
    if rssi >= -50: return Fore.GREEN
    if rssi >= -70: return Fore.YELLOW
    return Fore.RED


def ascii_preview(hexdata, width=16):
    if len(hexdata) % 2:
        hexdata = hexdata[:-1]
    out = []
    for i in range(0, len(hexdata), 2):
        c = chr(int(hexdata[i:i+2], 16))
        out.append(c if 32 <= ord(c) < 127 else ".")
        if len(out) >= width:
            break
    return "".join(out)


def parse(line):
    m = LINE_RE.match(line)
    if not m:
        return None
    mac, rssi, ln, data, uptime = m.groups()
    return {
        "mac": mac,
        "rssi": int(rssi),
        "len": int(ln),
        "data": data,
        "uptime": int(uptime),
    }


def open_port(port, baud):
    return serial.Serial(port, baud, timeout=0.2)


def detect_port():
    for p in serial.tools.list_ports.comports():
        if p.device.startswith(("/dev/ttyUSB", "/dev/ttyACM")) or \
           p.device.startswith("COM"):
            return p.device
    return None


# Terminal in cbreak mode so keys (n/d/q) respond immediately, without Enter.
_TTY_ORIG = None


def _enter_cbreak():
    global _TTY_ORIG
    if sys.stdin.isatty():
        import termios, tty
        _TTY_ORIG = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin)


def _restore_tty():
    global _TTY_ORIG
    if _TTY_ORIG is not None:
        try:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, _TTY_ORIG)
        except Exception:
            pass
        _TTY_ORIG = None


def _read_note(prompt="  note: "):
    """Read a note line char-by-char (cbreak: no Enter needed to start)."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    chars = []
    while True:
        c = sys.stdin.read(1)
        if c in ("\n", "\r"):
            break
        if c == "\x03":            # Ctrl+C
            raise KeyboardInterrupt
        chars.append(c)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(chars).strip()


def _brightness_bar(pct, width=10):
    """Render a small text brightness bar, e.g. '█████░░░░░'."""
    try:
        pct = max(0, min(100, int(pct)))
    except (TypeError, ValueError):
        pct = 0
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _tag_fields(lp):
    """Return the interpreted command fields for a tagged packet's CSV row."""
    fl = lp.get("fields") or {}
    return [fl.get("family", ""), fl.get("device", ""), fl.get("group", ""),
            fl.get("state", ""), fl.get("brightness", ""), fl.get("rgb", ""),
            fl.get("color", ""), fl.get("channels", "")]


def _kbd_thread(stop, state, tagw, tagf):
    """Owns stdin so keys respond instantly, independent of serial traffic.
    Shared `state` carries the dedup flag and the last BRMesh packet."""
    while not stop.is_set():
        k = sys.stdin.read(1)
        if not k:
            break
        if k in ("d", "D"):
            state["dedup"] = not state["dedup"]
            print(f"  >> dedup={'ON' if state['dedup'] else 'OFF'}",
                  file=sys.stderr, flush=True)
        elif k in ("n", "N"):
            note = _read_note()
            lp = state["last_packet"]
            if lp is None:
                print("  >> nothing to tag yet", file=sys.stderr, flush=True)
            else:
                tagw.writerow([
                    lp["ts"], lp["mac"], note, lp["data"],
                    lp["header"], lp["payload"], lp["tail"],
                ] + _tag_fields(lp))
                tagf.flush()
                print(f"  >> tagged {lp['mac']}: {note}", file=sys.stderr, flush=True)
        elif k in ("q", "Q"):
            stop.set()
            break


def _cksum(p):
    """Exploratory checksum candidates for a decrypted payload (for --checksum)."""
    def sumx(x): return sum(x) & 0xFF
    def carry(x):
        s = 0
        for b in x:
            s += b
            s = (s & 0xFF) + (s >> 8)
        return s & 0xFF
    def xorx(x):
        v = 0
        for b in x:
            v ^= b
        return v
    def crc8(x):
        crc = 0
        for b in x:
            crc ^= b
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        return crc
    return {"sum": sumx(p), "carry": carry(p), "xor": xorx(p), "crc8": crc8(p)}


class DisplayState:
    """Mutable state shared by the per-line renderer (dedup, per-MAC colors).

    Kept as a small object so both the serial monitor and monitor_remote.py can
    reuse handle_line() with independent state.
    """
    def __init__(self, mesh_key, filter_oui=False, checksum=False):
        self.mesh_key = mesh_key
        self.filter_oui = filter_oui
        self.checksum = checksum
        self.last_data = {}        # mac -> last DATA (dedup)
        self.last_mac = {}
        self.color_cycle = 0
        self.total = 0


def handle_line(raw, st, tagw=None, logf=None, verblogf=None, tracker=None,
                state=None):
    """Render one raw `BLE|...` line: filter, dedup, color, decode, log, track.

    Shared by monitor.py (serial) and monitor_remote.py (UDP). Returns the
    parsed packet dict if one was displayed, else None.
    """
    # Alive heartbeat: not a packet — show as status only.
    if raw.startswith("BLE_HEARTBEAT"):
        print(f"  {Style.DIM}· alive {raw.split('|UPTIME=')[-1]} ms{Style.RESET_ALL}",
              file=sys.stderr, flush=True)
        return None

    if logf:
        logf.write(raw + "\n")
        logf.flush()

    parsed = parse(raw)
    if parsed is None:
        return None

    # -- OUI filter ------------------------------------------------
    if st.filter_oui and not any(
        parsed["mac"].startswith(o)
        for o in ("a4:c1:38", "50:ec:50", "08:3a:f2", "e8:db:84")
    ):
        return None

    # Dedup: suppress unless DATA changed for this MAC.
    if state and state.get("dedup") and \
            st.last_data.get(parsed["mac"]) == parsed["data"]:
        return None
    st.last_data[parsed["mac"]] = parsed["data"]

    # Per-device stable color.
    if parsed["mac"] not in st.last_mac:
        st.last_mac[parsed["mac"]] = PALETTE[st.color_cycle % len(PALETTE)]
        st.color_cycle += 1
    color = clr(st.last_mac[parsed["mac"]])

    st.total += 1
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    up = f"{parsed['uptime']/1000:.1f}s"
    data_col = parsed["data"][:40] + ("..." if len(parsed["data"]) > 40 else "")
    row_plain = (f"{ts:<13} {parsed['mac']:<17} ({parsed['rssi']:>3}) "
                 f"{parsed['len']:<4} {up:<9} {data_col:<44} "
                 f"{ascii_preview(parsed['data']):<16}")
    print(
        f"{ts:<13} "
        f"{color(parsed['mac']):<17}{Fore.RESET} "
        f"{rgb(parsed['rssi'])}({parsed['rssi']:>3}){Fore.RESET} "
        f"{parsed['len']:<4} "
        f"{up:<9} "
        f"{data_col:<44} "
        f"{ascii_preview(parsed['data']):<16}",
        flush=True,
    )
    if verblogf:
        verblogf.write(row_plain + "\n")

    # BRMesh decode view -------------------------------------
    info = parse_brmesh(parsed["data"], st.mesh_key)
    if info:
        if state is not None:
            state["last_packet"] = {
                "ts": ts, "mac": parsed["mac"], "data": parsed["data"],
                "header": info.get("header", ""),
                "payload": info.get("decrypted", ""),
                "tail": info.get("tail", ""),
                "fields": info.get("fields", {}),
            }
        # Line 1: header fields
        if "header" in info:
            h = info
            l1 = (f"hdr={h['header']} fwd={h['forward']} cmd={h['cmd']} "
                  f"dev={h['i2']} seq={h['sequence']} "
                  f"cksum={h['checksum']:#04x}")
        else:
            l1 = f"hdr=? body={info['body_len']}B"
        print(f"  {Style.DIM}└ BRMesh {l1}{Style.RESET_ALL}", flush=True)
        if verblogf:
            verblogf.write("  └ BRMesh " + l1 + "\n")

        # Line 2: decoded payload + raw tail
        l2 = []
        if "decrypted" in info:
            l2.append(f"payload={info['decrypted']}")
            if st.checksum:
                pb = bytes.fromhex(info["decrypted"])
                ck = _cksum(pb)
                l2.append("ck=" + " ".join(f"{k}:{v:02X}" for k, v in ck.items()))
        if "tail" in info and info["tail"]:
            l2.append(f"tail={info['tail']}")
        if "type" in info:
            l2.append(f"type={info['type']}")
        if "reply_key" in info:
            l2.append(f"key={info['reply_key']}")
        if l2:
            print(f"  {Style.DIM}└          {' '.join(l2)}{Style.RESET_ALL}", flush=True)
            if verblogf:
                verblogf.write("  └          " + " ".join(l2) + "\n")

        # Line 3: interpreted command fields
        f = info.get("fields")
        if f:
            parts = [f"opcode={f.get('opcode','?')}"]
            for key in ("family", "device", "group", "channels", "state"):
                if key in f:
                    val = f[key]
                    # device/group are integers; show hex (matches the mesh's
                    # device ids) with the decimal in brackets, e.g. 0x34 (52)
                    if key in ("device", "group") and isinstance(val, int):
                        val = f"0x{val:02X} ({val})"
                    parts.append(f"{key}={val}")
            if "brightness" in f:
                b = f["brightness"]
                parts.append(f"brightness={b}% [{_brightness_bar(b)}]")
            if "rgb" in f:
                parts.append(f"rgb={f['rgb']}")
            if "w" in f:
                parts.append(f"white={f['w']}")
            if "color" in f:
                parts.append(f"color={f['color']}")
            line3 = " ".join(parts)
            print(f"  {Style.DIM}└          {line3}{Style.RESET_ALL}", flush=True)
            if verblogf:
                verblogf.write("  └          " + line3 + "\n")
                verblogf.flush()
            if tracker:
                tracker.update(f, ts, parsed["mac"])
                tracker.save()

    return parsed


def main():
    ap = argparse.ArgumentParser(description="BRMesh BLE sniff monitor")
    ap.add_argument("--port", help="serial port (auto-detect if omitted)")
    ap.add_argument("--filter-oui", action="store_true",
                    help="only show packets from known mesh OUIs")
    ap.add_argument("--key", metavar="HEX", default=None,
                    help="4-byte mesh key (e.g. 12345678) to decrypt payloads")
    ap.add_argument("--checksum", action="store_true",
                    help="show candidate checksums for each payload (exploratory)")
    args = ap.parse_args()

    try:
        mesh_key = hex_key(args.key) if args.key else None
    except ValueError as e:
        sys.exit(f"Bad --key: {e}")
    if mesh_key:
        print(f"Mesh key: {mesh_key.hex().upper()}", file=sys.stderr)

    port = args.port or detect_port()
    if not port:
        sys.exit("No ESP32 serial port found. Pass --port or plug one in.")

    # Logs are split into per-mesh subfolders by key; "unknown" if no key set.
    key_tag = mesh_key.hex().upper() if mesh_key else "unknown"
    LOG_DIR = os.path.join("logs", key_tag)
    os.makedirs(LOG_DIR, exist_ok=True)

    logname = os.path.join(LOG_DIR,
                           f"brmesh_sniff_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    logf = open(logname, "a")
    print(f"Logging to {logname} (Ctrl+C to quit)", file=sys.stderr)

    # Verbose log: mirrors the CLI display (decoded view) for later inspection.
    verblogname = os.path.join(LOG_DIR,
                               f"brmesh_verbose_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    verblogf = open(verblogname, "a")
    print(f"Verbose -> {verblogname}", file=sys.stderr)

    # Tagged-packet dataset (press n to tag the last BRMesh packet).
    tagname = os.path.join(LOG_DIR,
                           f"brmesh_tags_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
    tagf = open(tagname, "w", newline="")
    tagw = csv.writer(tagf)
    tagw.writerow(["time", "mac", "note", "data", "header", "payload", "tail",
                   "family", "device", "group", "state", "brightness",
                   "rgb", "color", "channels"])
    print(f"Tags -> {tagname} (press n to tag last packet)", file=sys.stderr)

    # Mesh structure accumulator (keyed by mesh key -> same file updated).
    tracker = None
    if mesh_key:
        mesh_path = os.path.join(LOG_DIR, f"brmesh_mesh_{mesh_key.hex().upper()}.json")
        tracker = MeshTracker(mesh_key, mesh_path)
        tracker.save()
        print(f"Mesh structure -> {mesh_path}", file=sys.stderr)

    # Shared state between the main (serial) loop and the keyboard thread.
    state = {"dedup": False, "last_packet": None}
    stop = threading.Event()

    # Per-line render state (dedup, per-MAC colors) reused by handle_line().
    st = DisplayState(mesh_key, filter_oui=args.filter_oui,
                      checksum=args.checksum)

    # Headers (in fixed columns; DATA column variable width).
    print("Controls: d=dedup  n=tag last  q=quit")
    print(f"{'TIME':<13} {'MAC':<17} {'RSSI':<5} {'LEN':<4} {'UP':<9} {'DATA':<44} {'ASCII':<16}")

    # Immediate keystrokes (cbreak) handled in a background thread so 'n'
    # responds the moment you press it, even with no serial traffic.
    _enter_cbreak()
    if sys.stdin.isatty():
        threading.Thread(target=_kbd_thread, args=(stop, state, tagw, tagf),
                         daemon=True).start()

    while True:
        try:
            ser = serial.Serial(port, BAUD, timeout=0.2)
            print(f"Connected: {port}", file=sys.stderr)
        except serial.SerialException:
            print(f"Waiting for {port} ...", file=sys.stderr)
            time.sleep(2)
            continue

        try:
            while True:
                if stop.is_set():
                    break
                raw = ser.readline().decode(errors="replace").strip()
                if not raw:
                    continue
                handle_line(raw, st, tagw=tagw, logf=logf, verblogf=verblogf,
                            tracker=tracker, state=state)
                # Quit requested from the keyboard thread.
                if stop.is_set():
                    break

        except serial.SerialException:
            print(f"Lost connection on {port} — retrying ...", file=sys.stderr)
            time.sleep(2)
        finally:
            try:
                ser.close()
            except Exception:
                pass
        if stop.is_set():
            break
        time.sleep(2)

    # Clean shutdown (q was pressed).
    try:
        verblogf.close()
        tagf.close()
        logf.close()
    except Exception:
        pass
    if tracker:
        tracker.save()
    _restore_tty()
    print("Bye.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _restore_tty()
        print("\nBye.")
        sys.exit(0)
