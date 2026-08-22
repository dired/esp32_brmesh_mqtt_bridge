#!/usr/bin/env python3
"""Re-render a raw brmesh_sniff_*.log as a monitor-style capture using a given
mesh key (corrects captures that were run with the wrong --key). Alive/heartbeat
lines are dropped; only real BRMesh packets are shown.

Usage:
    python3 convert_capture.py logs/brmesh_sniff_YYYYMMDD_HHMMSS.log \
        --key 12345678 --out "capture.decoded.txt"
"""
import argparse
import os
import re
import sys

from brmesh_lib import hex_key, parse_brmesh

LINE_RE = re.compile(
    r"^BLE\|MAC=([0-9a-f:]+)\|RSSI=(-?\d+)\|LEN=\d+\|DATA=([0-9A-F]*)\|UPTIME=(\d+)$"
)


def main():
    ap = argparse.ArgumentParser(description="Re-render a BRMesh capture with a key")
    ap.add_argument("log", help="path to brmesh_sniff_*.log")
    ap.add_argument("--key", required=True, help="mesh key hex (e.g. 12345678)")
    ap.add_argument("--out", required=True, help="output file")
    args = ap.parse_args()

    key = hex_key(args.key)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)

    rows = []
    for line in open(args.log):
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        mac, rssi, data, uptime = m.groups()
        info = parse_brmesh(data, key)
        if not info:
            continue
        rows.append((mac, rssi, uptime, info))

    with open(args.out, "w") as fo:
        fo.write(f"# BRMesh capture re-rendered with key {key.hex().upper()}\n")
        fo.write(f"# source: {args.log}\n")
        fo.write(f"# packets: {len(rows)}\n\n")
        for mac, rssi, uptime, info in rows:
            parts = [f"{mac} ({rssi}dBm) uptime={uptime}ms",
                     f"hdr={info.get('header','?')}",
                     f"payload={info.get('decrypted','')}"]
            fld = info.get("fields")
            if fld:
                sub = [f"{k}={v}" for k, v in fld.items()
                       if k in ("opcode", "family", "device", "state", "brightness")]
                if sub:
                    parts.append(" ".join(sub))
            fo.write("  ".join(parts) + "\n")
    print(f"wrote {len(rows)} packets -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
