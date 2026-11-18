#!/usr/bin/env python3
"""Local backup + export of the WHOOP database.

- Timestamped SQLite snapshot (consistent copy via the online backup API)
- JSON export of every table
- CSV export of the key health tables
Keeps the last 30 snapshots (7 when the daemon takes one a day); older ones are pruned.
Run standalone, from the collector, or from the daemon.
"""
import sqlite3, os, json, csv, time, glob, re, subprocess, sys, datetime as dt
import db

HERE = os.path.dirname(os.path.abspath(__file__))
BK = os.path.join(HERE, "backups")
KEY = os.path.join(HERE, ".backup_key")
os.makedirs(BK, exist_ok=True)

def _encrypt(path):
    """AES-256 encrypt a file with .backup_key, remove the plaintext. Returns .enc path."""
    if not os.path.exists(KEY):
        return path
    enc = path + ".enc"
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                    "-in", path, "-out", enc, "-pass", f"file:{KEY}"], check=True)
    os.remove(path)
    return enc

def decrypt(enc_path, out_path=None):
    out_path = out_path or enc_path[:-4]
    subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                    "-in", enc_path, "-out", out_path, "-pass", f"file:{KEY}"], check=True)
    print("decrypted ->", out_path)

def prune(keep=30):
    """Keep the newest `keep` snapshots; remove all files (.db/.json/.csv, encrypted
    or not) belonging to older ones."""
    stamp_of = lambda p: (re.search(r"-(\d{8}-\d{6})\.", os.path.basename(p)) or [None, None])[1]
    files = [p for p in glob.glob(os.path.join(BK, "*")) if stamp_of(p)]
    keep_stamps = set(sorted({stamp_of(p) for p in files})[-keep:])
    for p in files:
        if stamp_of(p) not in keep_stamps:
            os.remove(p)

TABLES = ["api_sleep", "api_recovery", "api_cycle", "api_workout",
          "api_profile", "ble_hr", "ble_raw", "clock_sync", "raw_archive", "daily_night", "sync_state"]

def snapshot(keep=30):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    src = db.connect()
    # consistent .db copy
    dst_path = os.path.join(BK, f"whoop-{stamp}.db")
    dst = sqlite3.connect(dst_path)
    src.backup(dst); dst.close()
    # JSON (all tables except the bulky raw frames, which the .db snapshot already holds)
    data = {t: [dict(r) for r in src.execute(f"SELECT * FROM {t}").fetchall()] for t in TABLES if t != "ble_raw"}
    json_path = os.path.join(BK, f"whoop-{stamp}.json")
    json.dump(data, open(json_path, "w"), indent=1)
    # CSV (health tables)
    for t in ("api_recovery", "api_sleep", "api_cycle", "api_workout"):
        rows = data[t]
        if rows:
            with open(os.path.join(BK, f"{t}-{stamp}.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=rows[0].keys()); w.writeheader(); w.writerows(rows)
    # encrypt everything just written (if a key exists)
    enc = os.path.exists(KEY)
    if enc:
        dst_path = _encrypt(dst_path)
        json_path = _encrypt(json_path)
        for f in glob.glob(os.path.join(BK, f"*-{stamp}.csv")):
            _encrypt(f)
    # prune to the newest `keep` snapshots (encrypted or not)
    prune(keep)
    sizes = sum(os.path.getsize(p) for p in glob.glob(os.path.join(BK, "*")))
    tag = "encrypted" if enc else "plaintext"
    print(f"backup {stamp} ({tag}): {sum(len(v) for v in data.values())} rows -> {dst_path}")
    print(f"  json: {json_path}  | backups dir: {sizes//1024} KB")
    return stamp, json_path

if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "decrypt":
        decrypt(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    else:
        snapshot()
