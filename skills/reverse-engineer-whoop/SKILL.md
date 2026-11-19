---
name: reverse-engineer-whoop
description: >
  Stand up a local-first WHOOP data system on someone's own machine, from zero: read
  their WHOOP 4.0 strap directly over Bluetooth (live heart rate, RR intervals, raw
  history, battery, firmware logs), optionally pull their scored history from the
  official WHOOP API, store everything in SQLite, run the full-screen localhost
  dashboard (Heart, Today, Archive), and keep it syncing and backed up. Use when a user
  wants to own their WHOOP data, read the strap without the app, build a WHOOP
  dashboard, or reproduce github.com/tohmsc/openvitals-public. Targets WHOOP 4.0 (BLE service
  61080001-8d6d-82b8-614a-1c8cb0f8dcc6); notes where 5.0/MG differ.
---

# Reverse-engineer your WHOOP (own your data)

This skill rebuilds a working system that already exists: the code is
[github.com/tohmsc/openvitals-public](https://github.com/tohmsc/openvitals-public), folder `whoop-tools/`.
You do not need to reverse engineer the protocol again. Your job is to install it on
the user's machine, prove each layer works, and adapt it to what they have.

What they end up with:

- Live heart rate and RR intervals straight off the strap, no WHOOP app involved.
- The strap's stored history drained losslessly into SQLite, plus battery and the
  decoded firmware console.
- Optionally, their scored WHOOP history (recovery, HRV, sleep, strain, workouts)
  from the official API, kept as a dated archive.
- A full-screen dashboard on `http://localhost:8787` with a Resync button.
- An hourly background collector and encrypted local backups. Cloudflare D1 off-site
  backup only if they want it.

## Read first, and tell the user

- **Their own strap, their own data.** Personal interoperability and backup. Never point
  this at a device they don't own.
- **Not a medical device.** Self-tracking only, no clinical decisions.
- **The strap path is reverse engineered, so it can break.** A firmware update may change
  the protocol. The official API is the stable path.
- **Scores are computed in WHOOP's cloud.** Recovery, strain, sleep stages and the HRV
  baseline are not on the strap. The strap gives raw signals: HR, RR, accelerometer, raw
  optical, battery, logs. The API needs a WHOOP account, and it only returns data that
  has reached WHOOP's servers (the strap must sync through the WHOOP phone app for new
  scores to appear).
- **One Bluetooth bond at a time.** Close the WHOOP phone app while this code talks to
  the strap.
- **Apple Health is iOS only.** HealthKit can't be written from macOS or Python.

## Before you start: ask three things

1. **Which strap?** WHOOP 4.0 is supported. 5.0/MG use a different service (`fd4b0001-…`)
   and CRC16: the frame builder must be adapted first (`resources/PROTOCOL.md`).
2. **Do they want their WHOOP account history too?** If yes, they need a WHOOP developer
   app (free) and an account. If no, skip phase 5; everything else works.
3. **Do they want off-site backup?** Default is local encrypted backups only. Cloudflare
   D1 is optional (`resources/CLOUDFLARE.md`).

Run `python skills/reverse-engineer-whoop/scripts/doctor.py whoop-tools` at any point. It
is read-only, prints PASS/WARN/FAIL for every layer, and says the next command.

## Phases (each has a "done when")

Full commands and platform notes are in `resources/SETUP.md`.

### 1. Get the code and dependencies
```bash
git clone https://github.com/tohmsc/openvitals-public.git && cd openvitals-public/whoop-tools
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```
Done when: `doctor.py` shows Python 3.10+ and all four packages PASS.

### 2. Show them the finished thing first (demo data)
```bash
.venv/bin/python seed_demo.py                                # fictional "Demo User"
WHOOP_DB=data/whoop-demo.db .venv/bin/python dashboard.py    # http://localhost:8787
.venv/bin/python seed_demo.py --stream                       # optional: fake live strap
```
Done when: they've seen the Heart view beating and agreed this is what they want. Stop
the demo server before phase 3. Never mix demo and real data: sync is disabled on any
database whose filename contains "demo".

### 3. Find the strap and prove the command path
- Grant the terminal Bluetooth permission (macOS: System Settings > Privacy & Security >
  Bluetooth). Close the WHOOP phone app. Strap on the wrist or the charger, nearby.
- `.venv/bin/python ble_scan.py` lists devices. **Match on the service UUID, never the
  name.** Users rename straps, and in the original build the phone and the strap had the
  same custom name, so name matching connected to the phone.
- `.venv/bin/python whoop.py selftest` proves the frame builder byte for byte against
  real captures. Then `.venv/bin/python whoop.py info` reads battery and firmware.

Done when: `whoop.py info` prints a battery percentage and a firmware version.

### 4. Live data and history off the strap
- `.venv/bin/python live.py` streams 1 Hz heart rate and RR into `ble_hr` and writes the
  `live_hr_now` marker the dashboard reads. The strap must be worn for a real pulse.
- `.venv/bin/python collector.py --live 20` drains stored history (incremental, every
  frame stored raw first in `ble_raw`), streams 20 s of live HR, then decodes.
- `.venv/bin/python decode.py` turns raw frames into `realtime_hr`, `device_soc`
  (battery), `device_logs` (firmware console) and `strap_samples` (raw optical, kept raw).

Done when: `doctor.py` shows strap heart-rate samples and raw frames above zero.

### 5. Optional: their WHOOP account history (official API)
- Create an app at developer.whoop.com. Redirect URI exactly
  `http://localhost:8765/callback`. Scopes: read:recovery, read:cycles, read:sleep,
  read:workout, read:profile, read:body_measurement, offline.
- `cp .env.example .env` and paste the Client ID and Secret. Never print or commit them.
- `.venv/bin/python whoop_api.py login` (browser, once), then `whoop_api.py sync`.

Done when: `doctor.py` shows recovery scores above zero. If the newest score is old, the
account simply has nothing newer (check by syncing the strap in the WHOOP phone app).

### 6. The dashboard
`.venv/bin/python dashboard.py` → `http://localhost:8787`. Details in `resources/DASHBOARD.md`.

- **Heart** (landing, full screen): a 3D heart beating on the strap's real beat timing
  (RR intervals when live, else 60/HR), callouts tied to the mesh, exploded view (`E`),
  full screen (`F`), drag to rotate, a pulse strip labelled as synthesized from beat
  timing (it is not an ECG).
- **Today**: only strap data since midnight, compared against the archive baseline.
- **Archive**: the WHOOP API history behind a calendar, always labelled with its date and
  marked as not today. Never present archive scores as today's.
- **Resync** (sidebar, Today, Heart toolbar): runs one collector cycle.

Done when: Today shows today's strap samples, and the calendar opens archive days (if
they did phase 5).

### 7. Keep it running and backed up
- `openssl rand -hex 32 > .backup_key`. Tell the user to save a copy: without it the
  encrypted backups can't be opened.
- launchd (macOS): the plists contain `__WHOOP_TOOLS_DIR__`. Fill in the absolute path
  while copying (commands in `whoop-tools/README.md`), then `launchctl load` both:
  `com.whoop.collector` hourly, `com.whoop.dashboard` always on.
- Linux: a systemd user timer for `collector.py` and a service for `dashboard.py`
  (`resources/SETUP.md`).

Done when: `doctor.py` shows both services loaded and a backup appears in `backups/`.

## The protocol in one screen
Full detail in `resources/PROTOCOL.md`.

- **Frame:** `AA | LEN(u16 LE = len(inner)+4) | CRC8(LEN) | inner | CRC32(inner, LE)`,
  `inner = [type][seq][cmd][data]`.
- **The CRCs are standard:** CRC-8/SMBUS (poly 0x07) over the two length bytes,
  CRC-32/ISO-HDLC (`binascii.crc32`) over `inner`. Claims of a "custom CRC" are wrong.
- **GATT:** write to `…0002`; notifications on `…0003`, `…0004`, `…0005`, `…0007`.
- **Commands:** ToggleGenericHrProfile `0x0E`, ToggleRealtimeHr `0x03`, SendHistoricalData
  `0x16` + ACK `0x17`, GetBattery `0x1A`, ReportVersion `0x07`, SetClock `0x0A`.

## Rules for the agent

- Verify each phase with `doctor.py` before starting the next.
- Store raw first, decode later. Never drop frames you can't parse yet.
- Don't label raw sensor fields you haven't verified with a controlled capture.
- Never print `.env`, `.tokens.json` or `.backup_key` contents, and never commit `whoop.db`,
  exports or backups (`.gitignore` already blocks them; keep it that way).
- Show data age honestly. Strap data is "today"; API data is a dated archive.
- When something fails, check `resources/TROUBLESHOOTING.md` before improvising.

## User has a different device, or no strap

Skip the Bluetooth phases and use the device adapter. Convert their export into
`whoop-tools/templates/daily.csv`, `workouts.csv` and `heart_rate.csv` following
`templates/README.md` (column units, plus Oura, Garmin, Apple Health and Fitbit mappings).
Then run `import_data.py --dir my_data --dry-run`, fix the rows it skips, import for real,
and start `dashboard.py`. Their filled CSVs and `whoop.db` stay out of git; `.gitignore`
already handles that.

## Resources

| File | What it covers |
| --- | --- |
| `resources/SETUP.md` | Every command from a blank machine, macOS and Linux, plus services |
| `resources/PROTOCOL.md` | Frame format, CRCs, packet types, command table, 5.0/MG notes |
| `resources/DATA-MODEL.md` | Every SQLite table, what fills it, how fresh it is |
| `resources/DASHBOARD.md` | Endpoints, Today vs Archive model, how to adapt the UI |
| `resources/TROUBLESHOOTING.md` | Symptom, cause and fix for everything that went wrong |
| `resources/CLOUDFLARE.md` | Optional Cloudflare D1 off-site backup |
| `resources/WALKTHROUGH.md` | How the original build went, as prompts you can reuse |
| `scripts/doctor.py` | Read-only preflight and progress check |
| `../../whoop-tools/templates/README.md` | Device adapter: load any wearable's export into the dashboard |
| `images/` | Screenshots of the dashboard with synthetic demo data |
