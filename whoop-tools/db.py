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

CREATE TABLE IF NOT EXISTS sync_state (key TEXT PRIMARY KEY, value TEXT);
"""

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
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
