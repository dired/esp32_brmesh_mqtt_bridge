#!/usr/bin/env python3
"""Re-decode a raw brmesh_sniff_*.log with a mesh key.

Prints a corrected readable capture (correct payloads for the given key) and
rebuilds/updates the mesh structure JSON using the same MeshTracker as the
live monitor. This fixes logs that were captured with the wrong --key.

Usage:
    python3 decode_log.py logs/brmesh_sniff_YYYYMMDD_HHMMSS.log --key 12345678
"""
import argparse
import os
import re
import sys

from brmesh_lib import hex_key, parse_brmesh, MeshTracker

LINE_RE = re.compile(
    r"^BLE\|MAC=([0-9a-f:]+)\|RSSI=-?\d+\|LEN=\d+\|DATA=([0-9A-F]*)\|UPTIME=(\d+)$"
)


def main():
    ap = argparse.ArgumentParser(description="Re-decode a BRMesh sniff log")
    ap.add_argument("log", help="path to brmesh_sniff_*.log")
    ap.add_argument("--key", required=True, help="mesh key hex (e.g. 12345678)")
    ap.add_argument("--out", help="corrected capture output (default: <log>.decoded.txt)")
    ap.add_argument("--mesh", help="mesh structure JSON (default: logs/brmesh_mesh_<KEY>.json)")
    args = ap.parse_args()

    key = hex_key(args.key)
    out = args.out or (args.log + ".decoded.txt")
    mesh_path = args.mesh or os.path.join(
        "logs", key.hex().upper(), f"brmesh_mesh_{key.hex().upper()}.json")
    os.makedirs(os.path.dirname(mesh_path), exist_ok=True)

    tracker = MeshTracker(key, mesh_path)
    n = 0
    with open(out, "w") as fo:
        for line in open(args.log):
            m = LINE_RE.match(line.strip())
            if not m:
                continue
            mac, data, uptime = m.groups()
            info = parse_brmesh(data, key)
            if not info:
                continue
            n += 1
            fields = info.get("fields", {})
            tracker.update(fields, f"{uptime}ms", mac)

            parts = [f"{mac} hdr={info.get('header','?')}",
                     f"payload={info.get('decrypted','')}",
                     f"tail={info.get('tail','')}"]
            if fields:
                parts.append(" ".join(f"{k}={v}" for k, v in fields.items()))
            print("  ".join(parts), file=fo)
    tracker.save()

    print(f"decoded {n} BRMesh packets -> {out}", file=sys.stderr)
    print(f"mesh structure -> {mesh_path} ({len(tracker.data['lights'])} lights, "
          f"{tracker.data['command_count']} cmds)", file=sys.stderr)


if __name__ == "__main__":
    main()
