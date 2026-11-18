# whoop-local

Your own local WHOOP data system. No cloud, no subscription dependency for the raw
signals; everything lives in `whoop.db` on your Mac.

**Two data sources → one SQLite DB → localhost dashboard**

1. **Strap over BLE** (reverse-engineered WHOOP 4.0 protocol) — live HR + RR (HRV),
   and a lossless capture of the historical drain. The laptop is the strap's host:
   `daemon.py` connects whenever the strap is nearby. Files: `whoop.py`, `daemon.py`.
2. **Official WHOOP API v2 (OAuth)** — Recovery, HRV, resting HR, SpO₂, skin temp,
   Sleep stages, Strain, workouts. File: `whoop_api.py`.

## One-time setup

```bash
cd whoop-tools
# 1. Create your WHOOP developer app at https://developer.whoop.com
#    Redirect URI MUST be exactly: http://localhost:8765/callback
#    Scopes: recovery, cycles, sleep, workout, profile, body measurement, offline
cp .env.example .env            # then paste your Client ID + Secret into .env
.venv/bin/python whoop_api.py login     # opens browser once, stores refresh token
.venv/bin/python whoop_api.py sync      # pulls your history into whoop.db
```

## Preview with synthetic demo data (no real biometrics)
```bash
.venv/bin/python seed_demo.py                              # generates data/whoop-demo.db (fictional)
WHOOP_DB=data/whoop-demo.db .venv/bin/python dashboard.py  # http://localhost:8787
.venv/bin/python seed_demo.py --stream                     # optional: fake a live strap (HR/RR every second)
```
The app opens on the full-screen **Heart** view. **Today** shows only strap data recorded
today; **Archive** holds the WHOOP cloud history behind a calendar. **Resync** (sidebar,
Today, Heart toolbar) runs one collector cycle. Set `WHOOP_HOME` if `.env`/`.tokens.json`
live in a different folder from the dashboard.
The real pipeline creates your own `whoop.db` (gitignored). Never commit a real `whoop.db`
or its exports — `.gitignore` blocks `*.db`, `data/`, `.env`, `.tokens.json`, `.backup_key`.

## Daily use

```bash
# always-on: hold the strap whenever it is nearby (drain + live HR), sync the API on a timer
.venv/bin/python daemon.py

# or a single collection cycle (strap drain + 20s live HR + API sync):
.venv/bin/python collector.py --live 20

# dashboard:
.venv/bin/python dashboard.py           # http://localhost:8787
```

### How the always-on daemon works (store and forward)
The strap records one sample a second into its own memory whether or not anything is
connected. `daemon.py` makes the laptop its host:

- **Strap in range:** connect, drain everything it stored while you were away, then keep
  the link open — live heart rate every second, and another drain every 5 minutes.
- **Out of range, laptop asleep, strap rebooted:** the daemon keeps looking and reconnects
  on its own. Nothing is lost in between; the strap hands the gap over on reconnect and
  `decode.py` backfills those seconds into `ble_hr` with the strap's own timestamps.
- **On timers, strap or not:** WHOOP API sync (hourly), decode (5 min), backup (daily,
  keeps 7), cloud push (hourly; `--no-cloud` if a separate `cloud_sync.py` job does it),
  and — if `CF_R2_BUCKET` is set — moving old raw frames to R2 and deleting them locally
  (hourly, see Backups).

The strap talks to one host at a time, so while the daemon holds it the official WHOOP
phone app cannot connect, and history the daemon drains is removed from the strap — the
phone app will not get it, so WHOOP's cloud scores (Recovery / Sleep / Strain) stop
updating for those periods. `daemon.py --no-drain` streams live only and leaves the
stored history on the strap for the phone app.

The strap only stores usable history while its clock reads real time. If it has been
reset (it then counts from 1971), drains return firmware logs but no heart rate. The
daemon sends SetClock on every connect, but it is not verified that this alone fixes a
reset clock; connecting the official WHOOP app once does. `collector.py` and the dashboard's **Resync** notice a running daemon and ask it
to drain right away instead of competing for the strap. `live.py` is the older
stream-only tool; the daemon replaces it (don't run both).

Tests (fake strap, no Bluetooth needed): `.venv/bin/python -m unittest test_daemon`
![Dashboard overview (synthetic demo data)](../skills/reverse-engineer-whoop/images/02-dashboard.png)

## Run it automatically in the background

The plists contain a `__WHOOP_TOOLS_DIR__` placeholder. Fill in the absolute path of
this folder while copying them (run from inside `whoop-tools/`):
```bash
sed "s|__WHOOP_TOOLS_DIR__|$PWD|g" com.whoop.daemon.plist > ~/Library/LaunchAgents/com.whoop.daemon.plist
launchctl load ~/Library/LaunchAgents/com.whoop.daemon.plist
# logs: daemon.out.log / daemon.err.log
# to stop: launchctl unload ~/Library/LaunchAgents/com.whoop.daemon.plist
```
launchd starts it at login and restarts it if it ever exits. If you used the older hourly
job, turn it off — the daemon does everything it did:
`launchctl unload ~/Library/LaunchAgents/com.whoop.collector.plist`
(`com.whoop.collector.plist` is still here if you prefer one cycle an hour.)

## Backups
- **Local:** the daemon writes a timestamped snapshot to `backups/` once a day and keeps the
  last 7 (a collector run writes one each time and keeps 30): `.db` + JSON + CSV,
  AES-256 encrypted with `.backup_key` if present. The JSON leaves out the raw strap
  frames (`ble_raw`); the `.db` snapshot has them. Restore with:
  `.venv/bin/python backup.py decrypt backups/whoop-YYYYMMDD-HHMMSS.json.enc out.json`
  **Keep a copy of `.backup_key`** — without it the encrypted backups can't be opened.
- **Cloud (Cloudflare D1):** add `CF_API_TOKEN` to `.env` (token with Account > D1 > Edit)
  and `CF_D1_DATABASE_ID` (create a D1 database, e.g. `whoop-backup`, in your Cloudflare account),
  then `.venv/bin/python cloud_sync.py` pushes everything to it (heart rate incrementally).
  Runs automatically, hourly from the daemon or each collector cycle, once the token is set.

- **Raw frames to Cloudflare R2, then cleaned up locally (`archive.py`):** the lossless raw
  frames in `ble_raw` grow by roughly 20 MB a day. With `CF_R2_BUCKET` set in `.env`, the
  daemon moves every complete hour older than `CF_R2_KEEP_HOURS` (default 24) to R2 once an
  hour: gzip, AES-256 with `.backup_key`, upload through the Cloudflare CLI, download again
  and compare, and only then delete those rows locally. A failed upload or check deletes
  nothing. Decoded data (heart rate, battery, logs) always stays local.
  ```bash
  npx wrangler login                                    # once
  npx wrangler r2 bucket create whoop-raw-archive       # once; then add CF_R2_BUCKET=whoop-raw-archive to .env
  .venv/bin/python archive.py status                    # what is in R2, what is still local
  .venv/bin/python archive.py restore out.db --from 2026-11-18T10 --to 2026-11-18T14   # get frames back (UTC)
  ```
  The archive is encrypted with `.backup_key`. **If that key is lost, the archived frames
  cannot be read** — keep a copy of it somewhere other than this laptop.

## Telegram: morning message, nudges, status
`telegram_report.py` talks to you through your own Telegram bot.

**Every morning** (after `TELEGRAM_DAILY_HOUR`, default 08:00; on wake if the laptop slept), one
short message that leads with how to treat yourself today:
```
🔴 Looks like you're strained. Take it lighter today. Yesterday was a big day, so that tracks.

• Night: resting HR 57, 8 above your usual. HRV 52 ms, lower than usual.
• Yesterday was a big day: 1h 20m with your heart rate up, peak 158.
• Strap battery 18%, charge it today.
```
The call is 🟢 push, 🟡 moderate or 🔴 lighter, from last night's resting heart rate and HRV
(22:00–06:00) against your usual, as learned from your own strap nights (see "It learns you"
below). No night data means no call. It is a rule of thumb for how hard to push,
**not medical advice**, and works without a WHOOP membership.

If the laptop slept through the night, the message waits (up to 45 minutes after you open the
lid) until the strap has handed over the night, rather than reporting "no data" straight away.

**Nudges**, sent once when they start and never between 22:00 and the morning hour: battery
under 20%, strap charged again, strap not seen for 6 hours, strap back, strap clock reset,
cloud archive stuck for days.

**Commands** you can send the bot: `/status` (is everything running), `/now` (today so far),
`/good` `/ok` `/rough` (how you feel today) and `/learned` (what it knows about you).
Replies take up to about 30 seconds. If the daemon itself is not running, nothing answers.

### It learns you
`learn.py` learns from three places.

**Your WHOOP archive** (the days WHOOP itself scored), refitted whenever the archive changes:
- **The recovery model.** How far WHOOP's recovery score moved, for you, when your HRV and
  resting HR moved away from your usual. The same relationship turns a strap night into a
  recovery estimate in percent. It is checked on the last 30% of archive days, which the
  weights used for that check never saw, and `python learn.py` reports the result (how often it
  lands in WHOOP's zone, and by how many points it is typically off).
- **Your hours.** When you actually fell asleep and woke, so "last night" covers your night
  instead of a fixed 22:00–06:00, and the morning message waits until your night is over.
- **Your range.** WHOOP's max heart rate for you, so "heart rate up" starts halfway between
  your resting rate and your max.
- **Your variation.** How much your resting HR and HRV normally move night to night. The
  estimate works in those units, so the same +5 bpm means more for a steady heart.

**Your strap nights** (`daily_night`, one row per morning): your usual is the median of your
last 30 nights, from the third night on. The archive is not used as your usual once it is more
than 60 days old (bodies change, and the strap's HRV is not on WHOOP's scale); it is shown for
comparison instead. Resting HR clearly above usual three mornings running is called out.

**You.** Tell it how you feel (`/good`, `/ok`, `/rough` on Telegram, or the buttons under Today
on the dashboard). Called 🔴 on a day you felt good, the red zone shrinks a little; called 🟢
on a day you felt rough, the green zone does. The zones start on WHOOP's (green from 67%, red
under 34%) and are recomputed from all your answers each time.

A night whose numbers no heart produces (resting HR under 30 or over 100) is reported as a bad
reading, not as a verdict, and is not learned from. None of this detects or diagnoses anything.

`python learn.py` prints everything it currently believes; nothing is hidden in a model file.

```bash
# 1. create a bot with @BotFather and put TELEGRAM_BOT_TOKEN=... in .env
.venv/bin/python telegram_report.py pair      # 2. prints a link: open it and press Start
.venv/bin/python telegram_report.py --print   # see the morning text without sending
.venv/bin/python telegram_report.py status    # see the /status text
```
The chat that sends the pairing code becomes the only recipient; everyone else is ignored. Note
this sends a health summary to Telegram's servers; bot chats are not end-to-end encrypted.

## Your dashboard on your phone (Cloudflare Worker, optional)

The same app, hosted on your own Cloudflare account and reading the D1 database
`cloud_sync.py` fills. Behind a password; installable to your home screen.

```bash
# 1. D1 off-site sync must work first (see Backups): CF_API_TOKEN + CF_D1_DATABASE_ID in .env
.venv/bin/python cloud_sync.py            # full push (creates/updates the tables)
# 2. Deploy (needs `npx wrangler login`, or a token with Workers Scripts:Edit + D1:Edit)
cd worker && ./deploy.sh                  # prints https://vitals.<you>.workers.dev
#    The password is generated once into .dash_password (read it there; never committed).
# 3. Keep the phone current: push every 3 min (full push every 25 min)
sed "s|__WHOOP_TOOLS_DIR__|$PWD/..|g" ../com.whoop.cloudsync.plist > ~/Library/LaunchAgents/com.whoop.cloudsync.plist
launchctl load ~/Library/LaunchAgents/com.whoop.cloudsync.plist
```
On the phone: open the URL, sign in, then Share > Add to Home Screen. It opens full screen
with a tab bar (Heart, Today, Archive, Device). The phone can't reach the strap, so data is
as fresh as the Mac's last push; run `live.py` on the Mac for continuous heart rate.
Change the password: edit `.dash_password`, then `npx wrangler secret put DASH_PASSWORD < ../.dash_password`.

## Always-on services (launchd)
- `com.whoop.daemon` — always on: holds the strap when nearby (drain + live HR), plus API sync, decode, backup, cloud push and R2 archive + local cleanup on timers
- `com.whoop.collector` — the older alternative: one cycle an hour (don't load both)
- `com.whoop.dashboard` — keeps `http://localhost:8787` up (auto-restarts)
```bash
# from inside whoop-tools/ (the placeholder becomes this folder's absolute path)
for p in com.whoop.daemon com.whoop.dashboard; do
  sed "s|__WHOOP_TOOLS_DIR__|$PWD|g" $p.plist > ~/Library/LaunchAgents/$p.plist
done
launchctl load ~/Library/LaunchAgents/com.whoop.daemon.plist
launchctl load ~/Library/LaunchAgents/com.whoop.dashboard.plist
```

## Files
| File | Role |
|---|---|
| `whoop.py` | WHOOP 4.0 BLE protocol (frames, CRCs, parsers) + CLI (`info`/`live`/`drain`) |
| `db.py` | SQLite schema + helpers |
| `whoop_api.py` | OAuth2 + v2 API sync |
| `daemon.py` | always-on strap host: connect when nearby, drain, live stream, reconnect; housekeeping on timers |
| `decode.py` | decode new raw strap frames → stored HR history (backfills gaps), battery SOC, firmware logs |
| `collector.py` | one collection cycle (BLE drain + live + API + decode + backup + cloud) |
| `archive.py` | move old raw frames to Cloudflare R2 (verified), delete them locally; `status`, `restore` |
| `test_daemon.py` | tests for the daemon and decoder with a fake strap |
| `telegram_report.py` | Telegram bot: morning message with a recommendation, nudges (battery, strap missing), commands, how-you-feel feedback |
| `learn.py` | what it has learned about you: recovery model fitted on your WHOOP archive, your usual, hours and range, zones tuned by your feedback |
| `metrics.py` | the numbers computed from strap heart rate (resting HR, HRV, time with HR up); shared by dashboard, Telegram and learn |
| `config.py` | paths and `.env` reading, shared by every script |
| `test_archive.py` | tests for the archive with a fake bucket |
| `test_telegram.py` | tests for the daily summary with a fake Telegram |
| `backup.py` | encrypted local snapshots (+ `decrypt`) |
| `cloud_sync.py` | push to Cloudflare D1 over REST |
| `dashboard.py` + `static/` | localhost dashboard: Flask JSON API + static page (vendored Chart.js, offline) |
| `seed_demo.py` | generates a synthetic demo DB (fictional "Demo User") for previews |
| `history_*.ndjson` / `ble_raw` | lossless raw strap frames for future deeper decoding |

## Notes / honesty
- Reverse-engineered personal tool, **not a medical device**. Good for fitness and
  self-tracking; don't use for clinical decisions.
- A WHOOP firmware update could change the BLE protocol; the API is the stable path.
- HRV is computed only from beat intervals that fit the heart rate the strap reports in the
  same second. While you move, the strap's beat timing is unreliable (most seconds carry no
  usable interval), so daytime HRV is an approximation; the night reading is the one to trust.
- While this laptop drains the strap, WHOOP's cloud receives nothing new, so the Recovery/
  Strain/Sleep archive stops where the WHOOP app last synced.
- Recovery/Strain/Sleep scores come from the **API** (WHOOP computes them in-cloud);
  the strap gives the raw signals those are derived from.
- Apple Health can't be read/written from macOS (HealthKit is iOS-only). To fold in
  existing Health history, export it from the iPhone Health app and parse the XML.
