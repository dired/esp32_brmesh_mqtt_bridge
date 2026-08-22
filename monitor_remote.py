#!/usr/bin/env python3
"""monitor_remote.py — BRMesh sniffer over WiFi/UDP (no serial cable).

Receives the `BLE|...` lines that the WiFi sniffer firmware
(esp32_sniffer/src/ble_bridge_wifi.ino) broadcasts via UDP, and renders
them exactly like `monitor.py` (same decode, dedup, colors, logging, tag 'n',
mesh tracking). Just the transport differs: UDP instead of USB serial.

The sniffer announces itself via mDNS as `brmesh-sniffer.local` so the host
can find it by name instead of an IP.

Usage:
    python3 monitor_remote.py                 # listen on the Link-Local broadcast
    python3 monitor_remote.py --port 41234    # custom UDP port
    python3 monitor_remote.py --host 192.168.1.50
                                              # unicast to a host instead of broadcast
    python3 monitor_remote.py --key 12345678  # decrypt BRMesh payloads
    python3 monitor_remote.py --filter-oui

Defaults: bind 0.0.0.0:41234. If --host is given we send a unicast bind probe
(datagrams, not a TCP connection) — mostly useful with the broadcast disabled.

Keys:
    d  toggle dedup mode
    q  quit
"""

import argparse
import csv
import datetime
import os
import socket
import sys
import threading

try:
    from colorama import init
    init()
except ImportError:
    pass

from monitor import (DisplayState, handle_line, _enter_cbreak, _restore_tty,
                     _kbd_thread, MeshTracker)
from brmesh_lib import hex_key


def main():
    ap = argparse.ArgumentParser(description="BRMesh sniffer over WiFi/UDP")
    ap.add_argument("--port", type=int, default=41234, metavar="PORT",
                    help="UDP port to listen on (default 41234)")
    ap.add_argument("--host", default=None, metavar="IP",
                    help="if set, send a one-shot packet to this host instead "
                         "of the subnet broadcast (primarily for testing)")
    ap.add_argument("--filter-oui", action="store_true",
                    help="only show packets from known mesh OUIs")
    ap.add_argument("--key", metavar="HEX", default=None,
                    help="4-byte mesh key (e.g. 12345678) to decrypt payloads")
    args = ap.parse_args()

    try:
        mesh_key = hex_key(args.key) if args.key else None
    except ValueError as e:
        sys.exit(f"Bad --key: {e}")
    if mesh_key:
        print(f"Mesh key: {mesh_key.hex().upper()}", file=sys.stderr)

    # Logs/tags/mesh files land in the same per-key folder as the serial monitor.
    key_tag = mesh_key.hex().upper() if mesh_key else "unknown"
    LOG_DIR = os.path.join("logs", key_tag)
    os.makedirs(LOG_DIR, exist_ok=True)

    logname = os.path.join(LOG_DIR,
                           f"brmesh_remote_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    logf = open(logname, "a")
    print(f"Logging to {logname} (Ctrl+C to quit)", file=sys.stderr)

    verblogname = os.path.join(LOG_DIR,
                               f"brmesh_remote_verbose_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    verblogf = open(verblogname, "a")
    print(f"Verbose -> {verblogname}", file=sys.stderr)

    tagname = os.path.join(LOG_DIR,
                           f"brmesh_remote_tags_{datetime.datetime.now():%Y%m%d_%H%M%S}.csv")
    tagf = open(tagname, "w", newline="")
    tagw = csv.writer(tagf)
    tagw.writerow(["time", "mac", "note", "data", "header", "payload", "tail",
                   "family", "device", "group", "state", "brightness",
                   "rgb", "color", "channels"])
    print(f"Tags -> {tagname} (press n to tag last packet)", file=sys.stderr)

    tracker = None
    if mesh_key:
        mesh_path = os.path.join(LOG_DIR,
                                 f"brmesh_mesh_{mesh_key.hex().upper()}.json")
        tracker = MeshTracker(mesh_key, mesh_path)
        tracker.save()
        print(f"Mesh structure -> {mesh_path}", file=sys.stderr)

    state = {"dedup": False, "last_packet": None}
    stop = threading.Event()
    st = DisplayState(mesh_key, filter_oui=args.filter_oui)

    print("Controls: d=dedup  n=tag last  q=quit")
    print(f"Listening on UDP 0.0.0.0:{args.port} "
          f"(sniffer: brmesh-sniffer.local) ...", file=sys.stderr)

    _enter_cbreak()
    if sys.stdin.isatty():
        threading.Thread(target=_kbd_thread, args=(stop, state, tagw, tagf),
                         daemon=True).start()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", args.port))
    sock.settimeout(0.2)

    # Optional: probe a specific host so it knows we're here (fire one datagram).
    if args.host:
        try:
            sock.sendto(b"BRMESH_REMOTE_READY", (args.host, args.port))
        except OSError as e:
            print(f"  (no --host reply: {e})", file=sys.stderr)

    try:
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            if not data:
                continue
            raw = data.decode(errors="replace").strip()
            if not raw:
                continue
            handle_line(raw, st, tagw=tagw, logf=logf, verblogf=verblogf,
                        tracker=tracker, state=state)
    finally:
        try:
            sock.close()
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
