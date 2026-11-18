---
name: reverse-engineer-whoop
description: >
  Help someone pull their own data off a WHOOP strap and run it locally, so they
  own their biometrics without depending on the subscription app. Use when a user
  wants to read their WHOOP over Bluetooth, extract live heart rate / HRV / raw
  sensor data, sync their WHOOP account via the official API, build a local
  dashboard, or back the data up (locally + Cloudflare). Targets the WHOOP 4.0
  (BLE service 61080001-8d6d-82b8-614a-1c8cb0f8dcc6); notes where 5.0/MG differ.
---

# Reverse-engineer your WHOOP (own your data)

This skill reproduces a working, local-first WHOOP data system: live biometrics
pulled straight off the strap over Bluetooth, the user's full history from the
official WHOOP API, a localhost dashboard, and automatic encrypted + cloud backups.
The complete code is in the `whoop-tools/` directory of this repo — this file tells
an agent how to stand it up on someone else's machine and adapt it.

## Scope, honesty, and safety (read first, tell the user)
- **Their own device, their own data.** This is personal interoperability/backup, not
  cracking someone else's. Don't help point it at a device the user doesn't own.
- **Not a medical device.** Good for fitness/self-tracking; never for clinical decisions.
- **Reverse-engineered → fragile.** A WHOOP firmware update can change the BLE protocol.
  The official **API** is the stable path; the **strap BLE** path is the bonus.
- **Scored metrics (Recovery, Strain, Sleep stages, HRV baseline) are computed in
  WHOOP's cloud**, not on the strap. Get those from the API. The strap gives the raw
  signals (HR, RR/HRV, accelerometer, raw optical/PPG, SpO₂, skin temp).
- **One bond at a time.** The strap talks to one device at a time — the official phone
  app must be disconnected while your code syncs.
- **Platform reality:** Apple HealthKit is iOS-only and cannot be read/written from
  macOS or Python. "Sync to Apple Health" needs a native iOS app; this system is
  Mac/Python + localhost instead.

## Identify the hardware
- WHOOP 4.0 advertises a custom GATT service **`61080001-8d6d-82b8-614a-1c8cb0f8dcc6`**
  (the `6108xxxx-…` family = GEN_4). **Match on this service UUID, not the device name**
  — users often rename the strap, and a renamed strap can collide with other devices
  (in the original build, the user's iPhone and strap were given the same custom name; an earlier
  attempt connected to the phone by mistake). WHOOP 5.0/MG use `fd4b0001-…` and a
  different CRC (CRC16) — see `resources/PROTOCOL.md`.
- The USB-C charger shows up as an **FTDI FT230X UART** (`/dev/cu.usbserial-*`). That UART
  is the battery-pack telemetry link, **not** a login console — the strap is a bare-metal
  Nordic nRF MCU, no shell. Don't chase a shell there; go via BLE.

## The protocol (verified, CRC-checked)
Full detail in `resources/PROTOCOL.md`. Essentials:
- **Frame:** `AA | LEN(u16 LE = len(inner)+4) | CRC8(LEN) | inner | CRC32(inner, LE)`,
  `inner = [type][seq][cmd][data]`.
- **CRCs are STANDARD**, not custom: CRC-8/SMBUS (poly 0x07) over the 2 length bytes,
  CRC-32/ISO-HDLC (`binascii.crc32`) over `inner`. Blogs claiming a "custom CRC" are
  wrong — that misconception is why naive attempts fail. `whoop.py selftest` proves the
  builder against real captured frames.
- **GATT:** write commands to char `…0002`; notifications on `…0003/0004/0005/0007`.
- **Useful commands:** ToggleGenericHrProfile `0x0E` (standard HR broadcast on/off),
  ToggleRealtimeHr `0x03` (1 Hz realtime stream), SendHistoricalData `0x16` + ACK `0x17`
  (incremental drain), GetBattery `0x1A`, ReportVersion `0x07`.

## Build procedure (what the agent does)
Work in a copy of `whoop-tools/`. Create a venv and `pip install -r requirements.txt`.

1. **Discover the strap.** `python whoop.py ...` / a BLE scan filtered by the service
   UUID. Confirm Device Info says `WHOOP Inc.` and battery reads.
2. **Prove the command path.** `python whoop.py info` → battery + firmware version.
3. **Live stream.** `python live.py` (writes `ble_hr` + a `live_hr_now` marker; dashboard
   shows a ● LIVE badge). The user must WEAR the strap for a real pulse — on the charger
   it reports a flat resting value.
4. **Drain history.** `python whoop.py drain` / the collector — incremental; stores raw
   frames losslessly (`ble_raw`, `history_*.ndjson`).
5. **Official API.** Create a WHOOP dev app at developer.whoop.com (redirect
   `http://localhost:8765/callback`, scopes recovery/cycles/sleep/workout/profile/
   body_measurement/offline). Put Client ID/Secret in `.env`. `python whoop_api.py login`
   then `sync`. This pulls the scored metrics into the same SQLite DB.
6. **Decode.** `python decode.py` turns raw frames into `realtime_hr`, `device_soc`
   (battery from firmware `SOC report` logs), `device_logs` (readable firmware console),
   and `strap_samples` (event #63 = `0x3F` R10/R11 raw optical/PPG — keep raw, don't
   over-label fields that aren't verified for this event format).
7. **Dashboard.** `python dashboard.py` → `http://localhost:8787` (Flask + SQLite,
   ~6 MB RAM; Chart.js vendored locally so it works offline).
8. **Automate + back up.** Load the two launchd agents (`com.whoop.collector` hourly:
   drain → decode → API → encrypted backup → cloud push; `com.whoop.dashboard` always-on).
   Local backups are AES-256 encrypted (`.backup_key` — tell the user to save a copy).
   Optional Cloudflare D1 off-site backup via `cloud_sync.py` (token with Account > D1 > Edit).

## Gotchas that cost time (so the next person skips them)
- Match on **service UUID**, never the advertised name.
- Standard HR characteristic (`0x2A37`) only emits after the custom enable command AND
  with skin contact.
- Realtime packet `ts` is the device RTC counter, not Unix; historical records use Unix.
  If the firmware logs "RTC timestamp invalid," the strap's clock was never set — send
  SetClock (`0x0A`).
- In a Python page-template string, `'\n'` becomes a real newline and breaks inline JS —
  escape as `\\n` (this silently blanked the whole dashboard once).
- Headless screenshots can fire before a CDN loads — vendor Chart.js locally.

## Files in `whoop-tools/`
`whoop.py` (protocol + CLI), `live.py` (live stream), `decode.py` (raw→structured),
`whoop_api.py` (OAuth + v2 API), `db.py`, `collector.py` (background), `backup.py`
(encrypted snapshots), `cloud_sync.py` (Cloudflare D1), `dashboard.py` (localhost),
launchd plists, `requirements.txt`, `.env.example`.

See `resources/PROTOCOL.md` (full protocol + command table) and `resources/WALKTHROUGH.md`
(the end-to-end prompts/path the original build followed).
