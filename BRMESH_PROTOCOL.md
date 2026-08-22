# BRMesh / Fastcon Protocol — Reverse-Engineering Notes

Status: **partially decoded**. Passive sniffing + decryption works, and the
single-light / group / group-colour command families are understood well enough
to control already-paired floodlights. This doc records the framing and decode
math so you can recreate the traffic on your own ESP32.

All examples below use the **placeholder key `12345678`** — substitute your own
mesh key (extracted via `adb logcat | grep jyq`, see the README).

## 1. Toolchain

```
ESP32 (passive BLE scanner) --WiFi/UDP--> monitor_remote.py (laptop)
```

- Sniffer firmware: `esp32_sniffer/src/ble_bridge_wifi.ino` (PlatformIO).
  - **Filter:** forwards only BRMesh — manufacturer signature `FF F0 FF`
    (company `0xfff0`) or known mesh OUIs. Drops Apple/other noise (`FF 4C 00`).
- `monitor_remote.py`: listens on UDP 41234; flags `--key`, `--filter-oui`.
- `brmesh_lib.py`: shared `parse_brmesh` / `decode_fields` / `MeshTracker`.
- `decode_log.py`, `convert_capture.py`: re-decode a raw log with a key.

**Packet line (from the sniffer):**

```
BLE|MAC=<mac>|RSSI=<rssi>|LEN=<len>|DATA=<hex>|UPTIME=<ms>
```

`DATA` = the full BLE advertising payload, e.g. `0201021BFFF0FF <body...>`.

## 2. On-the-wire advertisement structure

```
02 01 02                 BLE flags
1B FF F0 FF <body...>    AD len, manufacturer data, company 0xfff0, then body
```

- **Signature to filter on:** byte sequence `FF F0 FF`.
- After it comes the Fastcon **body** — 16 bytes (bridge firmware) or 24 bytes
  (the app; the extra 8 tail bytes are unencrypted).

## 3. Fastcon body layout (16 bytes)

| Offset | Field | Meaning |
|--------|-------|---------|
| 0 | `i2 \| (i<<4) \| (forward<<7)` | forward(bit7), command `i`(bits4-6), `i2`(bits0-3) |
| 1 | `sequence` | per-command counter |
| 2 | `safe_key` | `key[3]` (or 0xff if no key) |
| 3 | `checksum` | **sum of all other 15 bytes mod 256** (the ONLY protocol checksum) |
| 4..15 | `data` | command bytes + zero padding to 12 bytes |

**Encryption (XOR):**

```
body[0..3]  ^= default_key [0x5e, 0x36, 0x7b, 0xc4]
body[4..15] ^= mesh_key     (byte j XOR key[j&3], j=0..11)
```

**Decryption** = XOR the same keys back (what `brmesh_lib` does).

## 4. Command decoding

The app whitens the 16-byte Fastcon body (fixed LFSR stream seeded by
`whitening_init(0x25)`), then encrypts `body[4..15]` with the mesh key. The
sniffer's "decrypted" payload `p[0..11]` therefore maps each plaintext data
byte to a fixed output position XOR'd with a **key-dependent base** (computed
in `brmesh_lib._xorbases`).

The **plaintext command byte** is `data[0] = p[6] ^ fam_base`, and it is
**key-agnostic**:

| data[0] | family |
|---------|--------|
| `0x22` | single-light brightness |
| `0x72` | single-light colour |
| `0x43` | group / all on-off-brightness |
| `0x93` | group colour (RGBW) |

Layout after `p[6]`:

- **single-light** (`22`/`72`): `p[7]` = device id, `p[8]` = state/brightness
  (bit7 = ON, low7 = level 0..127).
- **group** (`43`): `p[7]` = group id, `p[10]` = `data[4] ^ base`
  (bit7 = ON flag, low7 = level 0..127; `0x00` = OFF, `0x80` = ON).
- **group-colour** (`93`): `p[7]` = group id, `p[10]` = brightness (bit7 = ON,
  low7 = level), `p[11]` = blue, and the raw tail bytes 0..3 =
  red / green / white / white2.

Device/group ids are recovered with `id = p[7] ^ id_base`; single-light
brightness with `level = (p[8] & 0x7F) ^ bright_base`; single-light colour with
`blue = p[9] ^ blue_base`, `red = p[10] ^ red_base`, `green = p[11] ^ green_base`.

The bases depend on the key — `brmesh_lib` computes them automatically, so the
monitor decodes correctly for any key you pass with `--key`.

## 5. Mesh structure JSON

`logs/<KEY>/brmesh_mesh_<KEY>.json` (built by `MeshTracker`, updated live):

```json
{
  "mesh_key": "12345678",
  "lights": { "34": { "device": 52, "last_mac": "...", "last_state": "ON" } },
  "group_events": [ { "state": "ON", "brightness": 100 } ],
  "groups_seen": ["42"],
  "command_count": 42
}
```

Rebuild from a raw log: `python3 decode_log.py <raw.log> --key 12345678`.

## 6. Recreating the mesh on your ESP32

The bridge firmware (`esp32_mqtt_bridge/src/main.cpp`) already implements this:

1. Keep the Fastcon framing + encryption (`package_ble_fastcon_body`,
   `whitening_encode`) as-is — they are confirmed correct.
2. Set the mesh key in `src/config.h` to the mesh you're joining.
3. Build the 12-byte command `data` with the layout in §4, then transmit via BLE
   advertising with manufacturer company `0xfff0` (prefix `FF F0 FF`).
4. Compare your transmitted `DATA` against what the sniffer records to confirm
   byte-for-byte parity with the app.

## 7. Remaining unknowns

- Full opcode set / layout for the `color2` and `music` families.
- Meaning of the app's extra 8 tail bytes (24-byte body vs 16).
- Purpose of payload byte `p[5]` (varies per packet; likely a nonce/sequence).
