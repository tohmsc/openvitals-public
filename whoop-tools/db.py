#!/usr/bin/env python3
"""SQLite store for WHOOP data from both the official API and the strap (BLE)."""
import sqlite3, os, json, time

DB_PATH = os.environ.get("WHOOP_DB") or os.path.join(os.path.dirname(__file__), "whoop.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_sleep (
  id TEXT PRIMARY KEY, start TEXT, end TEXT, nap INTEGER,
  resp_rate REAL, sleep_perf REAL, sleep_consistency REAL, sleep_efficiency REAL,
  total_in_bed_ms INTEGER, total_awake_ms INTEGER, total_light_ms INTEGER,
  total_sws_ms INTEGER, total_rem_ms INTEGER, disturbances INTEGER, raw TEXT);
CREATE TABLE IF NOT EXISTS api_recovery (
  cycle_id INTEGER, sleep_id TEXT, created_at TEXT, score_state TEXT,
  recovery_score REAL, resting_hr REAL, hrv_rmssd_ms REAL, spo2 REAL,
  skin_temp_c REAL, raw TEXT, PRIMARY KEY (cycle_id));
CREATE TABLE IF NOT EXISTS api_cycle (
  id INTEGER PRIMARY KEY, start TEXT, end TEXT, score_state TEXT,
  strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL, raw TEXT);
CREATE TABLE IF NOT EXISTS api_workout (
  id TEXT PRIMARY KEY, start TEXT, end TEXT, sport_name TEXT, score_state TEXT,
  strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL, distance_m REAL,
  zone_raw TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS api_profile (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS ble_hr (
  ts REAL PRIMARY KEY, hr INTEGER, rr_ms TEXT);
CREATE TABLE IF NOT EXISTS ble_raw (
  ts REAL, channel TEXT, ptype INTEGER, hex TEXT);
CREATE INDEX IF NOT EXISTS idx_ble_raw_ts ON ble_raw(ts);

-- Strap clock vs wall clock. The strap stamps stored records with its own RTC
-- counter, which is not Unix time and restarts when the strap resets. One row per
-- (re)connect or clock jump: wall time = rtc + offset.
CREATE TABLE IF NOT EXISTS clock_sync (
  ts REAL PRIMARY KEY, rtc INTEGER, offset REAL);

-- What the laptop was doing, written by daemon.py: 'linked' (connected to the strap),
-- 'searching' (awake, strap not connected: out of range or Bluetooth stuck) or 'asleep'.
-- No row = the daemon wasn't running. metrics.coverage explains gaps in ble_hr with it.
CREATE TABLE IF NOT EXISTS host_log (
  start REAL PRIMARY KEY, end REAL NOT NULL, state TEXT NOT NULL);

-- Raw frames that archive.py moved to Cloudflare R2 and removed from ble_raw:
-- one object per UTC hour (hour = unix time / 3600).
CREATE TABLE IF NOT EXISTS raw_archive (
  key TEXT PRIMARY KEY, hour INTEGER, first_rowid INTEGER, last_rowid INTEGER,
  rows INTEGER, bytes INTEGER, sha256 TEXT, encrypted INTEGER, archived_at REAL);

-- One row per morning: last night's resting HR and HRV as measured from the strap.
-- telegram_report.py uses the recent rows as "your usual".
-- learn.py adds what the day before looked like, the call it made, and how you said you felt
-- (feel: 1 rough, 2 ok, 3 good), and learns your usual and your thresholds from these rows.
-- n_rr: clean beat pairs behind the night's HRV (alerts.py only trusts HRV with enough of them).
CREATE TABLE IF NOT EXISTS daily_night (date TEXT PRIMARY KEY, rhr REAL, hrv REAL,
  load_s REAL, peak INTEGER, recorded_s REAL, score REAL, call TEXT, feel INTEGER, feel_ts REAL);

CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);

-- Requests for the strap from other programs (dashboard, CLI). Only the daemon holds the
-- Bluetooth link, so it runs these on its next tick (alarm.py). status: pending, done,
-- failed, expired.
CREATE TABLE IF NOT EXISTS strap_cmd (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, arg TEXT,
  status TEXT DEFAULT 'pending', result TEXT, done_ts REAL);
"""

# Columns added after a table first shipped: (table, column, type). Added on connect if missing.
ADDED = [("daily_night", col, typ) for col, typ in (
    ("load_s", "REAL"), ("peak", "INTEGER"), ("recorded_s", "REAL"), ("score", "REAL"),
    ("call", "TEXT"), ("feel", "INTEGER"), ("feel_ts", "REAL"), ("n_rr", "INTEGER"))]

def add_columns(c, added):
    """ALTER TABLE for each (table, column, type) the database doesn't have yet."""
    for table, col, typ in added:
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if have and col not in have:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError as ex:      # another process added it a moment ago
                if "duplicate column" not in str(ex):
                    raise

RR_FIX_VERSION = 1            # PRAGMA user_version from which stored beat intervals are true ms
RR_FIX_REPUSH_DAYS = 7        # the cloud only shows the last day of beats; re-send a week of them


def fix_rr_units(c):
    """Once: rescale beat intervals stored before the strap's RR was read as milliseconds.

    Until Nov 2026 the strap's RR values were treated as 1/1024 s (the standard Heart Rate
    service's unit) and stored as x * 1000/1024, 2.3% short. They are whole milliseconds:
    60000 / mean(RR) matches the strap's own heart rate to 0.3% at rest. Undo the scaling and
    queue the last week for cloud_sync. Every stored rr_ms came from the strap conversion
    (imports store ""), so all non-empty rows are rescaled.
    """
    # The marker is the file's user_version, not a sync_state row: clearing sync_state (tests,
    # a reset) must never scale the beats a second time.
    if c.execute("PRAGMA user_version").fetchone()[0] >= RR_FIX_VERSION:
        return 0
    c.execute("BEGIN IMMEDIATE")
    if c.execute("PRAGMA user_version").fetchone()[0] >= RR_FIX_VERSION:   # another process did it
        c.rollback()
        return 0
    rows = c.execute("SELECT ts, rr_ms FROM ble_hr WHERE rr_ms IS NOT NULL AND rr_ms != ''").fetchall()
    fixed = []
    for r in rows:
        try:
            vals = [round(float(x) * 1024 / 1000) for x in r["rr_ms"].split()]
        except ValueError:
            continue
        fixed.append((" ".join(map(str, vals)), r["ts"]))
    c.executemany("UPDATE ble_hr SET rr_ms = ? WHERE ts = ?", fixed)
    # HRV (RMSSD) computed from those beats scales the same way. scores.py recomputes its
    # last week on its own; these tables are written once and kept.
    for table, col in (("daily_night", "hrv"), ("sleep_night", "hrv"), ("stress", "rmssd")):
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            c.execute(f"UPDATE {table} SET {col} = {col} * 1.024 WHERE {col} IS NOT NULL")
    c.execute("CREATE TABLE IF NOT EXISTS cloud_hr_pending (ts REAL PRIMARY KEY)")
    c.execute("INSERT OR IGNORE INTO cloud_hr_pending(ts) SELECT ts FROM ble_hr "
              "WHERE ts >= ? AND rr_ms != ''", (time.time() - RR_FIX_REPUSH_DAYS * 86400,))
    c.execute(f"PRAGMA user_version = {RR_FIX_VERSION}")
    c.commit()
    return len(fixed)


def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    # WAL: the always-on daemon writes every second while the dashboard reads.
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    add_columns(c, ADDED)
    fix_rr_units(c)
    return c

def get_state(c, key, default=None):
    r = c.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_state(c, key, value):
    c.execute("INSERT INTO sync_state(key,value) VALUES(?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    c.commit()

def upsert(c, table, row: dict):
    cols = ",".join(row); ph = ",".join("?" * len(row))
    sets = ",".join(f"{k}=excluded.{k}" for k in row)
    pk = {"api_sleep":"id","api_recovery":"cycle_id","api_cycle":"id",
          "api_workout":"id","ble_hr":"ts","api_profile":"key"}.get(table, "id")
    c.execute(f"INSERT INTO {table}({cols}) VALUES({ph}) "
              f"ON CONFLICT({pk}) DO UPDATE SET {sets}", list(row.values()))

if __name__ == "__main__":
    c = connect()
    print("db ready at", DB_PATH)
    for t in ("api_sleep","api_recovery","api_cycle","api_workout","ble_hr","ble_raw"):
        n = c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
        print(f"  {t:14s} {n} rows")
