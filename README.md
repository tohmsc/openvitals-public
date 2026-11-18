# whoop-os — own your WHOOP data

A local-first system to pull your own biometrics off a **WHOOP 4.0**: live heart rate /
HRV straight off the strap over Bluetooth, your full history from the official WHOOP API,
a lightweight localhost dashboard, and automatic encrypted + cloud backups. Everything
stays on your machine.

> **Your device, your data.** This is personal interoperability and backup — not for any
> device you don't own. It's **not a medical device** (fitness/self-tracking only), it's
> reverse-engineered (a firmware update can change the protocol), and the computed scores
> (Recovery/Strain/Sleep) come from WHOOP's cloud via the API, not the strap.

## Give it to an AI (easiest path)
1. Clone this repo and open it in an AI coding agent (e.g. Claude Code) on the Mac with the
   strap nearby.
2. Point the agent at **`skills/reverse-engineer-whoop/SKILL.md`** (or copy that folder to
   `~/.claude/skills/` to make it a first-class skill), then say:
   > "Use the reverse-engineer-whoop skill to help me own my WHOOP 4.0 data."
3. The agent follows the skill: finds the strap by BLE service UUID, proves the command
   path, streams live HR, drains history, wires up the WHOOP API, and stands up the
   dashboard + backups.

## Do it yourself
```bash
cd whoop-tools
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python whoop.py selftest        # prove the protocol frame builder
.venv/bin/python daemon.py                # hold the strap while it's nearby: drain + live HR; Ctrl-C to stop
# official API: copy .env.example -> .env, add dev-app creds, then:
.venv/bin/python whoop_api.py login && .venv/bin/python whoop_api.py sync
.venv/bin/python dashboard.py             # http://localhost:8787
```
Full setup, backups, and the always-on background service are in **`whoop-tools/README.md`**.

The strap records one sample a second on its own. `daemon.py` connects whenever the strap
is near the laptop, collects what it stored while you were away, and streams live in
between, so the heart-rate record has no gaps even though the laptop is not always there.

## Preview with demo data (fill-in template)
This repo ships **synthetic demo data only** — a fictional "Demo User", no real person's
biometrics. See the dashboard populated before connecting anything:
```bash
cd whoop-tools
.venv/bin/python seed_demo.py                              # (re)generate data/whoop-demo.db
WHOOP_DB=data/whoop-demo.db .venv/bin/python dashboard.py  # http://localhost:8787
```
When you run the real pipeline it creates your **own** `whoop.db` (gitignored) and fills it
from your strap + API. The demo DB is just the template so you know what "done" looks like.

## The dashboard
A local, offline app (`whoop-tools/static/`, vendored Chart.js, light/dark theme):

- **Heart** (full screen, the landing view): a 3D heart that beats on the strap's real beat
  timing, metric callouts tied to the mesh, an exploded mode (`E`), full screen (`F`),
  drag to rotate, a live pulse strip and a timeline.
- **Today**: only what the strap recorded today (live monitor, heart rate over the day,
  daytime HRV from RR intervals), compared against your archive baseline.
- **Archive**: WHOOP cloud history behind a calendar. Every page is labelled with the day
  you're viewing: recovery, vitals, trends, sleep, activity and workouts for that day.
- **Device**: strap battery, stored record counts, decoded firmware log, and a **Resync**
  button that runs one collector cycle (or asks the running daemon to drain now).

Screenshots below use the synthetic demo data:

![Heart view](skills/reverse-engineer-whoop/images/02-dashboard.png)
![Today](skills/reverse-engineer-whoop/images/03-trends-sleep.png)
![Archive day](skills/reverse-engineer-whoop/images/04-activity-device.png)

## Do it yourself with an agent
Point your coding agent (Claude Code, Codex, Cursor) at
[`skills/reverse-engineer-whoop/SKILL.md`](skills/reverse-engineer-whoop/SKILL.md). It walks
from a blank machine to a running system in seven phases, each with a "done when" check, and
`skills/reverse-engineer-whoop/scripts/doctor.py` tells it what's done and what's next.
Setup, protocol, data model, dashboard, troubleshooting and the optional Cloudflare backup
are in `skills/reverse-engineer-whoop/resources/`.

## What's here
- `whoop-tools/` — the working system (BLE protocol, always-on daemon, API sync, decode,
  dashboard, encrypted backups, Cloudflare D1 sync, launchd services).
- `whoop-tools/templates/` + `import_data.py` — **device adapter**: no WHOOP 4.0? Fill the
  CSV templates from any wearable's export (Oura, Garmin, Apple Health, Fitbit…) and import
  them into the same dashboard. Mapping guide + agent prompt in `templates/README.md`.
- `skills/reverse-engineer-whoop/` — a portable skill + `resources/PROTOCOL.md`
  (verified protocol + command table) and `resources/WALKTHROUGH.md` (the end-to-end path).

## Architecture
```
 WHOOP 4.0 strap ──BLE──►  whoop.py / daemon.py ───────────────┐
 WHOOP API (OAuth) ──────►  whoop_api.py ─────────────────────┤──►  SQLite (whoop.db)
                                                              │        │
                                      decode.py (raw→tables) ─┘        ├─► dashboard.py (localhost)
                                                                       ├─► backup.py (encrypted local)
                                                                       ├─► cloud_sync.py (Cloudflare D1)
                                                                       └─► archive.py (raw frames → Cloudflare R2, then removed locally)
```
