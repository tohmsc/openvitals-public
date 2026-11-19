# Setup from a blank machine

Every command, in order. Run them from `openvitals-public/whoop-tools/` unless noted.
`doctor.py` checks each step: `python ../skills/reverse-engineer-whoop/scripts/doctor.py .`

## What you need

| Thing | Why | Notes |
| --- | --- | --- |
| A WHOOP 4.0 strap you own | the data source | 5.0/MG need protocol changes first |
| macOS 12+ or Linux with BlueZ | Bluetooth LE via `bleak` | Windows works with bleak too, but the services below are macOS/Linux |
| Python 3.10+ | everything is Python | `brew install python` or python.org |
| git, openssl | clone, encrypted backups | preinstalled on macOS |
| A WHOOP account + developer app | only for the optional API archive | free at developer.whoop.com |
| A Cloudflare account | only for optional off-site backup | see `CLOUDFLARE.md` |

## 1. Code and Python environment
```bash
git clone https://github.com/tohmsc/openvitals-public.git
cd openvitals-public/whoop-tools
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # bleak, Flask, pyserial, requests
```

## 2. Preview with demo data (optional, recommended)
```bash
.venv/bin/python seed_demo.py                                # writes data/whoop-demo.db
WHOOP_DB=data/whoop-demo.db .venv/bin/python dashboard.py    # http://localhost:8787
.venv/bin/python seed_demo.py --stream                       # second terminal: fake live strap
```
Ctrl-C both before moving on.

## 3. Bluetooth permission and the strap
- **macOS:** the first BLE scan triggers a permission prompt for your terminal app
  (Terminal, iTerm, Ghostty, VS Code...). If you missed it: System Settings > Privacy &
  Security > Bluetooth, enable your terminal, restart it.
- **Linux:** `sudo systemctl enable --now bluetooth`, and add yourself to the `bluetooth`
  group if scans return nothing.
- Close the WHOOP app on your phone (or turn the phone's Bluetooth off for a minute).
- Wear the strap, or put it on its charger nearby.

```bash
.venv/bin/python ble_scan.py          # look for service 61080001-8d6d-82b8-614a-1c8cb0f8dcc6
.venv/bin/python whoop.py selftest    # frame builder vs real captures, no strap needed
.venv/bin/python whoop.py info        # battery + firmware version from the strap
```
Optional: put the address `ble_scan.py` printed into `.env` as `WHOOP_BLE_ADDRESS=` to skip
discovery. On macOS this is a CoreBluetooth UUID, not a MAC address.

## 4. Live heart rate and history
```bash
.venv/bin/python live.py                    # continuous 1 Hz HR + RR, Ctrl-C to stop
.venv/bin/python collector.py --live 20     # drain history + 20 s live + API + decode + backup
.venv/bin/python decode.py                  # re-run decoding any time
```
`live.py` holds a `.live.lock` so the hourly collector skips Bluetooth while it runs.

## 5. Optional: WHOOP API archive
1. developer.whoop.com > create app.
2. Redirect URI exactly `http://localhost:8765/callback`.
3. Scopes: read:recovery, read:cycles, read:sleep, read:workout, read:profile,
   read:body_measurement, offline.
4. Then:
```bash
cp .env.example .env            # paste WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET
.venv/bin/python whoop_api.py login     # browser opens once; stores .tokens.json
.venv/bin/python whoop_api.py sync      # incremental from the last synced record
```

## 6. Dashboard
```bash
.venv/bin/python dashboard.py            # http://localhost:8787
PORT=8790 .venv/bin/python dashboard.py  # another port
WHOOP_HOME=/path/to/whoop-tools          # if .env/.tokens.json live elsewhere (Resync uses it)
```

## 7. Backups
```bash
openssl rand -hex 32 > .backup_key      # save a copy somewhere safe (password manager)
.venv/bin/python backup.py              # writes an encrypted snapshot to backups/, keeps 30
.venv/bin/python backup.py decrypt backups/whoop-YYYYMMDD-HHMMSS.json.enc out.json
```

## 8. Run it in the background

**macOS (launchd).** From inside `whoop-tools/`:
```bash
for p in com.whoop.collector com.whoop.dashboard; do
  sed "s|__WHOOP_TOOLS_DIR__|$PWD|g" $p.plist > ~/Library/LaunchAgents/$p.plist
  launchctl load ~/Library/LaunchAgents/$p.plist
done
# logs: collector.out.log / collector.err.log / dashboard.*.log
# stop: launchctl unload ~/Library/LaunchAgents/com.whoop.collector.plist
```

**Linux (systemd user units).** Example, adjust the path:
```ini
# ~/.config/systemd/user/whoop-collector.service
[Service]
WorkingDirectory=%h/openvitals-public/whoop-tools
ExecStart=%h/openvitals-public/whoop-tools/.venv/bin/python collector.py --live 20

# ~/.config/systemd/user/whoop-collector.timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target

# ~/.config/systemd/user/whoop-dashboard.service
[Service]
WorkingDirectory=%h/openvitals-public/whoop-tools
ExecStart=%h/openvitals-public/whoop-tools/.venv/bin/python dashboard.py
Restart=always
[Install]
WantedBy=default.target
```
```bash
systemctl --user daemon-reload
systemctl --user enable --now whoop-collector.timer whoop-dashboard.service
```

## Done checklist
- [ ] `doctor.py`: no FAIL lines
- [ ] `whoop.py info` prints battery and firmware
- [ ] Today view shows samples from today
- [ ] (optional) Archive calendar opens WHOOP days
- [ ] `.backup_key` saved somewhere outside this machine
- [ ] collector and dashboard services loaded
