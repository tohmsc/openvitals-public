#!/usr/bin/env python3
"""Sync the local WHOOP DB to Cloudflare D1 over the REST API (incremental).

Needs in .env:
  CF_API_TOKEN=...            # token with Account > D1 > Edit
  CF_D1_DATABASE_ID=<your-d1-database-id>   # e.g. a D1 DB named "whoop-backup"
The account id is auto-discovered from the token. Data goes straight to your
Cloudflare account; it never passes through any chat/LLM context.

Run:  python cloud_sync.py           # push everything new
"""
import os, sqlite3, requests, sys, time
import db

HERE = os.path.dirname(__file__)
CF = "https://api.cloudflare.com/client/v4"

SCHEMA = [
 "CREATE TABLE IF NOT EXISTS api_recovery (cycle_id INTEGER PRIMARY KEY, sleep_id TEXT, created_at TEXT, score_state TEXT, recovery_score REAL, resting_hr REAL, hrv_rmssd_ms REAL, spo2 REAL, skin_temp_c REAL)",
 "CREATE TABLE IF NOT EXISTS api_sleep (id TEXT PRIMARY KEY, start TEXT, end TEXT, nap INTEGER, resp_rate REAL, sleep_perf REAL, sleep_consistency REAL, sleep_efficiency REAL, total_in_bed_ms INTEGER, total_awake_ms INTEGER, total_light_ms INTEGER, total_sws_ms INTEGER, total_rem_ms INTEGER, disturbances INTEGER)",
 "CREATE TABLE IF NOT EXISTS api_cycle (id INTEGER PRIMARY KEY, start TEXT, end TEXT, score_state TEXT, strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL)",
 "CREATE TABLE IF NOT EXISTS api_workout (id TEXT PRIMARY KEY, start TEXT, end TEXT, sport_name TEXT, score_state TEXT, strain REAL, avg_hr REAL, max_hr REAL, kilojoule REAL, distance_m REAL)",
 "CREATE TABLE IF NOT EXISTS ble_hr (ts REAL PRIMARY KEY, hr INTEGER, rr_ms TEXT)",
 "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
]
COLS = {
 "api_recovery": "cycle_id,sleep_id,created_at,score_state,recovery_score,resting_hr,hrv_rmssd_ms,spo2,skin_temp_c",
 "api_sleep": "id,start,end,nap,resp_rate,sleep_perf,sleep_consistency,sleep_efficiency,total_in_bed_ms,total_awake_ms,total_light_ms,total_sws_ms,total_rem_ms,disturbances",
 "api_cycle": "id,start,end,score_state,strain,avg_hr,max_hr,kilojoule",
 "api_workout": "id,start,end,sport_name,score_state,strain,avg_hr,max_hr,kilojoule,distance_m",
 "ble_hr": "ts,hr,rr_ms",
}

def env():
    e = {}
    p = os.path.join(HERE, ".env")
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); e[k] = v
    return e

def esc(v):
    if v is None: return "NULL"
    if isinstance(v, (int, float)): return repr(v)
    return "'" + str(v).replace("'", "''") + "'"

class D1:
    def __init__(self, token, acct, dbid):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self.url = f"{CF}/accounts/{acct}/d1/database/{dbid}/query"
    def q(self, sql):
        r = self.s.post(self.url, json={"sql": sql}, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"D1 {r.status_code}: {r.text[:300]}")
        j = r.json()
        if not j.get("success"):
            raise RuntimeError(f"D1 error: {j.get('errors')}")
        return j

def discover_account(token):
    r = requests.get(f"{CF}/accounts", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    accts = r.json()["result"]
    if not accts:
        raise RuntimeError("token has no accounts")
    return accts[0]["id"]

def main():
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
    for s in SCHEMA:
        d1.q(s)
    c = db.connect()
    total = 0
    for table, cols in COLS.items():
        collist = cols.split(",")
        rows = c.execute(f"SELECT {cols} FROM {table}").fetchall()
        for i in range(0, len(rows), 100):
            chunk = rows[i:i+100]
            vals = ",".join("(" + ",".join(esc(r[k]) for k in collist) + ")" for r in chunk)
            d1.q(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES {vals};")
            total += len(chunk)
        print(f"  {table}: {len(rows)} rows")
    d1.q(f"INSERT OR REPLACE INTO meta (key,value) VALUES ('last_cloud_sync','{time.strftime('%Y-%m-%dT%H:%M:%S')}')")
    print(f"cloud sync complete: {total} rows -> D1 {dbid}")

if __name__ == "__main__":
    main()
