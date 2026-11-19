#!/usr/bin/env python3
"""Sync the local WHOOP DB to Cloudflare D1 over the REST API (incremental).

Needs in .env:
  CF_API_TOKEN=...            # token with Account > D1 > Edit
  CF_D1_DATABASE_ID=<your-d1-database-id>   # e.g. a D1 DB named "whoop-backup"
The account id is auto-discovered from the token. Data goes straight to your
Cloudflare account; it never passes through any chat/LLM context.

Run:
  python cloud_sync.py          # full: archive tables + everything new from the strap
  python cloud_sync.py --fast   # quick: only new strap heart rate, battery, logs, sync state
  python cloud_sync.py --auto   # what the launchd job runs: --fast, plus a full push every 25 min

The cloud dashboard (worker/) reads this database. Run --fast every few minutes
(launchd: com.whoop.cloudsync.plist) so your phone stays current.
Set WHOOP_HOME to read .env from another folder.
"""
import os, sqlite3, requests, sys, time, json
import alerts, config, db, metrics, scores, sleep, stress

HERE = config.HERE
HOME = os.environ.get("WHOOP_HOME") or HERE
CF = "https://api.cloudflare.com/client/v4"

EST_TABLE = ("CREATE TABLE IF NOT EXISTS estimates (date TEXT, kind TEXT, version TEXT, value REAL, ok INTEGER, "
             "why TEXT, row TEXT, inputs TEXT, computed_at REAL, PRIMARY KEY (date, kind, version))")
SCHEMA = [
 "CREATE TABLE IF NOT EXISTS api_recovery (cycle_id INTEGER PRIMARY KEY, sleep_id TEXT, created_at TEXT, score_state TEXT, recovery_score REAL, resting_hr REAL, hrv_rmssd_ms REAL, spo2 REAL, skin_temp_c REAL)",
 "CREATE TABLE IF NOT EXISTS api_sleep (id TEXT PRIMARY KEY, start TEXT, end TEXT, nap INTEGER, resp_rate REAL, sleep_perf REAL, sleep_consistency REAL, sleep_efficiency REAL, total_in_bed_ms INTEGER, total_awake_ms INTEGER, total_light_ms INTEGER, total_sws_ms INTEGER, total_rem_ms INTEGER, disturbances INTEGER)",
 "CREATE TABLE IF NOT EXISTS api_cycle (id INTEGER PRIMARY KEY, start TEXT, end TEXT, score_state TEXT, strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL)",
 "CREATE TABLE IF NOT EXISTS api_workout (id TEXT PRIMARY KEY, start TEXT, end TEXT, sport_name TEXT, score_state TEXT, strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL, distance_m REAL)",
 "CREATE TABLE IF NOT EXISTS api_profile (key TEXT PRIMARY KEY, value TEXT)",
 "CREATE TABLE IF NOT EXISTS ble_hr (ts REAL PRIMARY KEY, hr INTEGER, rr_ms TEXT)",
 "CREATE TABLE IF NOT EXISTS device_soc (rtc INTEGER PRIMARY KEY, soc_pct REAL, subsystem TEXT)",
 "CREATE TABLE IF NOT EXISTS device_logs (rtc INTEGER, subsystem TEXT, message TEXT, PRIMARY KEY (rtc, subsystem, message))",
 "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
 sleep.SCHEMA,
 EST_TABLE,
 stress.SCHEMA,
 "CREATE INDEX IF NOT EXISTS idx_ble_hr_ts ON ble_hr(ts)",
]
MIGRATIONS = ["ALTER TABLE api_workout ADD COLUMN zone_raw TEXT"]  # safe to fail if present

ARCHIVE = {
 "api_recovery": "cycle_id,sleep_id,created_at,score_state,recovery_score,resting_hr,hrv_rmssd_ms,spo2,skin_temp_c",
 "api_sleep": "id,start,end,nap,resp_rate,sleep_perf,sleep_consistency,sleep_efficiency,total_in_bed_ms,total_awake_ms,total_light_ms,total_sws_ms,total_rem_ms,disturbances",
 "api_cycle": "id,start,end,score_state,strain,avg_hr,max_hr,kilojoule",
 "api_workout": "id,start,end,sport_name,score_state,strain,avg_hr,max_hr,kilojoule,distance_m,zone_raw",
}
PROFILE_KEYS = ("first_name",)  # only what the dashboard shows
META_KEYS = ("last_ble_sync", "api_since", "live_hr_now", "battery_now", "pack_battery_now", "stress_baseline")


def env():
    return config.env(os.path.join(HOME, ".env"))


CTRL = {i: None for i in range(32) if i not in (9, 10)}  # strip control bytes (firmware text has some)


def esc(v):
    if v is None: return "NULL"
    if isinstance(v, (int, float)): return repr(v)
    if isinstance(v, bytes): v = v.decode("utf-8", "replace")
    return "'" + str(v).translate(CTRL).replace("'", "''") + "'"


class D1:
    def __init__(self, token, acct, dbid):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.url = f"{CF}/accounts/{acct}/d1/database/{dbid}/query"

    def q(self, sql, ok_fail=False, tries=3):
        # a dropped or slow link (e.g. right after the Mac wakes) gets two more tries;
        # every statement here is an upsert, so sending one twice is harmless
        for i in range(tries):
            try:
                r = self.s.post(self.url, json={"sql": sql}, timeout=60)
                if r.status_code < 500 or i == tries - 1:
                    break
            except (requests.ConnectionError, requests.Timeout):
                if i == tries - 1:
                    raise
            time.sleep(3 * (i + 1))
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or not j.get("success"):
            if ok_fail:
                return None
            raise RuntimeError(f"D1 {r.status_code}: {str(j.get('errors') or r.text)[:300]}")
        return j

    def upsert(self, table, cols, rows, chunk=100):
        collist = cols.split(",")
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            vals = ",".join("(" + ",".join(esc(r[k]) for k in collist) + ")" for r in part)
            self.q(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES {vals};")
        return len(rows)


def discover_account(token):
    r = requests.get(f"{CF}/accounts", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    accts = r.json()["result"]
    if not accts:
        raise RuntimeError("token has no accounts")
    return accts[0]["id"]


def has(c, t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def push_late_hr(c, d1, batch=5000):
    """Heart rate decode.py filled in behind the watermark (stored history read late): push
    those rows, then forget them. Returns how many went up."""
    if not has(c, "cloud_hr_pending"):
        return 0
    sent = 0
    while True:
        late = c.execute("SELECT p.ts, h.hr, h.rr_ms FROM cloud_hr_pending p LEFT JOIN ble_hr h ON h.ts = p.ts "
                         "ORDER BY p.ts LIMIT ?", (batch,)).fetchall()
        if not late:
            return sent
        rows = [r for r in late if r["hr"] is not None]
        if rows:
            d1.upsert("ble_hr", "ts,hr,rr_ms", rows, chunk=200)
        c.executemany("DELETE FROM cloud_hr_pending WHERE ts = ?", [(r["ts"],) for r in late])
        c.commit()
        sent += len(rows)


def main(fast=False):
    e = env()
    token = e.get("CF_API_TOKEN")
    dbid = e.get("CF_D1_DATABASE_ID")
    if not token or not dbid:
        print("Set CF_API_TOKEN and CF_D1_DATABASE_ID in .env first.")
        print("Create a token at https://dash.cloudflare.com/profile/api-tokens")
        print("  -> Custom token, permission: Account > D1 > Edit")
        return
    acct = e.get("CF_ACCOUNT_ID") or discover_account(token)
    d1 = D1(token, acct, dbid)
    c = db.connect()
    pushed = {}

    if not fast:
        for s in SCHEMA:
            d1.q(s)
        for m in MIGRATIONS:
            d1.q(m, ok_fail=True)
        for table, cols in ARCHIVE.items():
            rows = c.execute(f"SELECT {cols} FROM {table}").fetchall()
            pushed[table] = d1.upsert(table, cols, rows)
        prof = c.execute(f"SELECT key,value FROM api_profile WHERE key IN ({','.join('?' * len(PROFILE_KEYS))})", PROFILE_KEYS).fetchall()
        pushed["api_profile"] = d1.upsert("api_profile", "key,value", prof)

    # Strap heart rate: incremental from a watermark kept in the local DB.
    mark = float(db.get_state(c, "cloud_hr_ts", "0") or 0)
    if fast and mark == 0:
        mark = time.time() - 2 * 86400  # first fast run: last two days only
    hr = c.execute("SELECT ts,hr,rr_ms FROM ble_hr WHERE ts > ? ORDER BY ts", (mark,)).fetchall()
    pushed["ble_hr"] = d1.upsert("ble_hr", "ts,hr,rr_ms", hr, chunk=200)
    if hr:
        db.set_state(c, "cloud_hr_ts", str(hr[-1]["ts"]))
    pushed["ble_hr"] += push_late_hr(c, d1)

    if has(c, "device_soc"):
        soc = c.execute("SELECT rtc,soc_pct,subsystem FROM device_soc ORDER BY rtc DESC LIMIT 200").fetchall()
        pushed["device_soc"] = d1.upsert("device_soc", "rtc,soc_pct,subsystem", soc)
    if has(c, "device_logs"):
        logs = c.execute("SELECT rtc,subsystem,replace(message, char(0), '') AS message FROM device_logs ORDER BY rtc DESC LIMIT 120").fetchall()
        pushed["device_logs"] = d1.upsert("device_logs", "rtc,subsystem,message", logs)

    # last nights' sleep (sleep.py): small, so the last 30 go up every run
    if has(c, "sleep_night"):
        d1.q(sleep.SCHEMA)
        nights = c.execute(f"SELECT {sleep.COLS} FROM sleep_night ORDER BY date DESC LIMIT 30").fetchall()
        pushed["sleep_night"] = d1.upsert("sleep_night", sleep.COLS, nights)

    # strap score estimates (scores.py): every version, small, changes through the day
    if has(c, "estimates"):
        if not db.get_state(c, "cloud_est_schema"):        # a fast run can come before any full one
            d1.q(EST_TABLE)
            db.set_state(c, "cloud_est_schema", "1")
        est = c.execute("SELECT date,kind,version,value,ok,why,row,inputs,computed_at FROM estimates "
                        "WHERE computed_at > ?", (float(db.get_state(c, "cloud_est_ts", "0") or 0),)).fetchall()
        pushed["estimates"] = d1.upsert("estimates", "date,kind,version,value,ok,why,row,inputs,computed_at", est)
        if est:
            db.set_state(c, "cloud_est_ts", str(max(r["computed_at"] for r in est)))
    # stress monitor (stress.py): windows that changed since the last push (today and yesterday)
    if has(c, "stress"):
        if not db.get_state(c, "cloud_stress_schema"):
            d1.q(stress.SCHEMA)
            db.set_state(c, "cloud_stress_schema", "1")
        st = c.execute(f"SELECT {stress.COLS} FROM stress WHERE computed_at > ? AND date >= ?",
                       (float(db.get_state(c, "cloud_stress_ts", "0") or 0),
                        time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400)))).fetchall()
        pushed["stress"] = d1.upsert("stress", stress.COLS, st)
        if st:
            db.set_state(c, "cloud_stress_ts", str(max(r["computed_at"] for r in st)))
    meta = [{"key": k, "value": db.get_state(c, k)} for k in META_KEYS if db.get_state(c, k) is not None]
    # the stress baseline in use and the health alerts that are on (alerts.py), for the cloud app
    try:
        meta.append({"key": "health_alerts", "value": json.dumps(alerts.active(c))})
    except Exception as ex:
        print("alerts error:", ex)
    meta.append({"key": "scores_version", "value": scores.current(c)})
    meta.append({"key": "last_cloud_sync", "value": str(time.time())})
    counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("api_recovery", "api_sleep", "api_cycle", "api_workout", "ble_hr", "ble_raw")}
    meta.append({"key": "local_counts", "value": json.dumps(counts)})
    meta.append({"key": "coverage", "value": json.dumps(metrics.coverage_days(c, 7))})   # Device page
    d1.upsert("meta", "key,value", meta)
    c.commit()
    print("cloud sync complete:", ", ".join(f"{k} {v}" for k, v in pushed.items()), f"-> D1 {dbid[:8]}…")


def auto():
    """For the every-few-minutes job: quick push, plus a full push every 25 minutes."""
    c = db.connect()
    last_full = float(db.get_state(c, "cloud_full_at", "0") or 0)
    full = time.time() - last_full > 25 * 60
    main(fast=not full)
    if full:
        db.set_state(c, "cloud_full_at", str(time.time())); c.commit()


if __name__ == "__main__":
    if "--auto" in sys.argv:
        auto()
    else:
        main(fast="--fast" in sys.argv)
