# Troubleshooting

Symptom, likely cause, fix. Most of these happened during the original build.

## Bluetooth

| Symptom | Cause | Fix |
| --- | --- | --- |
| Scan finds nothing at all | Terminal has no Bluetooth permission (macOS) or BlueZ is off (Linux) | Grant permission in System Settings > Privacy & Security > Bluetooth and restart the terminal; `systemctl enable --now bluetooth` |
| Devices found, no WHOOP | Strap bonded to the phone, asleep, or out of range | Close the WHOOP phone app, wear or charge the strap within a metre, scan again |
| Connected, but Device Info says Apple / iPhone | Matched by name; phone and strap share a custom name | Match on service `61080001-8d6d-82b8-614a-1c8cb0f8dcc6`. `whoop.find()` already does |
| Writes succeed, no response | Frame built with a "custom" CRC from a blog | CRC-8/SMBUS over the length, CRC-32/ISO-HDLC over inner. Run `whoop.py selftest` |
| No heart rate on `0x2A37` | Standard HR profile is off by default | Send ToggleGenericHrProfile `0x0E` and wear the strap (skin contact) |
| HR stuck at one flat value | Strap on the charger, not on a wrist | Wear it |
| "strap not reachable this run" in the collector | Another process holds the strap (`live.py`, the phone app) | Normal. `live.py` creates `.live.lock` and the collector skips Bluetooth |
| Firmware log: "RTC timestamp invalid; not saving data to flash" | Strap clock never set | Send SetClock `0x0A` with the current time |
| WHOOP 5.0 / MG found | Different service (`fd4b0001-…`) and CRC16 | Adapt the frame builder first, see `PROTOCOL.md` |

## WHOOP API

| Symptom | Cause | Fix |
| --- | --- | --- |
| Login: redirect mismatch | Redirect URI in the developer app differs | Set it to exactly `http://localhost:8765/callback` |
| Login hangs | Port 8765 taken | Free the port, or change `WHOOP_REDIRECT_URI` in `.env` and the app to match |
| 401 after it worked | Refresh token expired or revoked | `whoop_api.py login` again |
| Sync works but nothing newer than some date | WHOOP's servers have nothing newer; the strap hasn't synced through the phone app | Open the WHOOP app, let it sync, then Resync. Check membership is active |
| 429 Too Many Requests | Rate limit | `whoop_api.py` already backs off; just rerun later |

## Dashboard

| Symptom | Cause | Fix |
| --- | --- | --- |
| "Couldn't load your data" | `dashboard.py` not running, or wrong `WHOOP_DB` | Start it; check the path |
| Port already in use | Another dashboard (often the launchd one) | `PORT=8790 .venv/bin/python dashboard.py`, or `lsof -i :8787` |
| Archive shows old scores | Expected: the archive is WHOOP's history | Look at Today for current strap data |
| Today chart is a few dots | The collector only streams 15-20 s of live HR per hourly run | Run `live.py` for continuous data, or raise `--live` |
| Resync button disabled | Demo database | Point `WHOOP_DB` at your real `whoop.db` |
| Resync says strap not in range | Strap asleep, far, or bonded to the phone | Bring it close and close the phone app |
| Daytime HRV says "needs more RR data" | Fewer than 10 clean RR differences today | Stream longer with `live.py` while sitting still |
| Blank page after editing a Python template | A `'\n'` became a real newline inside inline JS | The front end is static files now; keep JS out of Python strings |

## Backups

| Symptom | Cause | Fix |
| --- | --- | --- |
| Backups not encrypted | No `.backup_key` | `openssl rand -hex 32 > .backup_key` |
| Can't decrypt an old backup | Key lost or changed | Only the original `.backup_key` opens it. Save a copy off the machine |
