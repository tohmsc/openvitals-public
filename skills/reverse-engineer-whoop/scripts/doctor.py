#!/usr/bin/env python3
"""Preflight + progress check for a whoop-tools install. Read-only: it never writes
the database, never prints secret values and never sends anything to the strap.

Usage (from anywhere):
  python doctor.py [path/to/whoop-tools] [--no-ble]

Each line is PASS / WARN / FAIL with the next action. An agent can run this after
every phase of the skill to know what is done and what to do next.
"""
import os, sys, json, sqlite3, shutil, socket, importlib, subprocess, time

WHOOP4_SVC = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WHOOP5_PREFIX = "fd4b0001"
args = [a for a in sys.argv[1:] if not a.startswith("--")]
HOME = os.path.abspath(args[0] if args else os.path.join(os.path.dirname(__file__), "..", "..", "..", "whoop-tools"))
results = []


def say(level, what, fix=""):
    results.append(level)
    mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL", "INFO": "INFO"}[level]
    print(f"[{mark}] {what}" + (f"\n       -> {fix}" if fix else ""))


def env_keys(path):
    keys = {}
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = bool(v.strip()) and "your_" not in v and "<" not in v
    return keys


def main():
    print(f"whoop-tools folder: {HOME}\n")
    if not os.path.exists(os.path.join(HOME, "whoop.py")):
        say("FAIL", "whoop.py not found here", "pass the path to the whoop-tools/ folder: python doctor.py ~/openvitals-public/whoop-tools")
        return

    # 1. Python + packages
    v = sys.version_info
    say("PASS" if v >= (3, 10) else "FAIL", f"Python {v.major}.{v.minor}", "" if v >= (3, 10) else "install Python 3.10+ (python.org or `brew install python`)")
    venv = os.path.join(HOME, ".venv", "bin", "python")
    if not os.path.exists(venv):
        say("WARN", "no .venv in whoop-tools", "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
    missing = []
    for mod in ("bleak", "flask", "requests", "serial"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        say("WARN", f"packages not importable by this Python: {', '.join(missing)}",
            "run doctor with the venv Python (.venv/bin/python doctor.py ...) or pip install -r requirements.txt")
    else:
        say("PASS", "bleak, flask, requests, pyserial importable")
    say("PASS" if shutil.which("openssl") else "WARN", "openssl available (encrypted backups)",
        "" if shutil.which("openssl") else "install openssl, or backups stay unencrypted")

    # 2. Config (key names only, never values)
    keys = env_keys(os.path.join(HOME, ".env"))
    if not keys:
        say("WARN", "no .env yet (only needed for the WHOOP API)", "cp .env.example .env, then add Client ID/Secret from developer.whoop.com")
    else:
        for k in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET"):
            say("PASS" if keys.get(k) else "WARN", f".env has {k}", "" if keys.get(k) else "fill it from your app at developer.whoop.com")
        if keys.get("CF_API_TOKEN"):
            say("INFO", "Cloudflare D1 backup configured" + ("" if keys.get("CF_D1_DATABASE_ID") else " but CF_D1_DATABASE_ID is missing"))
    tok = os.path.join(HOME, ".tokens.json")
    say("PASS" if os.path.exists(tok) else "WARN", "WHOOP API login (.tokens.json)",
        "" if os.path.exists(tok) else ".venv/bin/python whoop_api.py login   (opens a browser once)")
    say("PASS" if os.path.exists(os.path.join(HOME, ".backup_key")) else "WARN", "backup key (.backup_key)",
        "" if os.path.exists(os.path.join(HOME, ".backup_key")) else "openssl rand -hex 32 > .backup_key  (then save a copy somewhere safe)")

    # 3. Strap over BLE (scan only, no connection)
    if "--no-ble" in sys.argv:
        say("INFO", "BLE scan skipped (--no-ble)")
    elif "bleak" in missing:
        say("WARN", "BLE scan skipped: bleak not importable")
    else:
        import asyncio
        from bleak import BleakScanner

        async def scan():
            return await BleakScanner.discover(timeout=6.0, return_adv=True)
        try:
            found = asyncio.run(scan())
            w4 = [(a, d, adv) for a, (d, adv) in found.items() if WHOOP4_SVC in [u.lower() for u in (adv.service_uuids or [])]]
            w5 = [(a, d, adv) for a, (d, adv) in found.items() if any(u.lower().startswith(WHOOP5_PREFIX) for u in (adv.service_uuids or []))]
            if w4:
                rssi = max(adv.rssi or -999 for _, _, adv in w4)
                say("PASS", f"WHOOP 4.0 strap advertising (RSSI {rssi} dBm)")
            elif w5:
                say("WARN", "WHOOP 5.0/MG found: different service + CRC16", "see resources/PROTOCOL.md (5.0/MG section); whoop.py targets 4.0")
            elif not found:
                say("FAIL", "BLE scan saw nothing at all", "grant Bluetooth permission to your terminal (System Settings > Privacy & Security > Bluetooth) and retry")
            else:
                say("WARN", f"{len(found)} BLE devices, no WHOOP strap advertising",
                    "wear or charge the strap nearby, and close the WHOOP phone app (the strap bonds to one device at a time)")
        except Exception as ex:  # noqa: BLE001
            say("FAIL", f"BLE scan failed: {ex}", "check Bluetooth is on and your terminal has Bluetooth permission")

    # 4. Database
    dbp = os.environ.get("WHOOP_DB") or os.path.join(HOME, "whoop.db")
    if not os.path.exists(dbp):
        say("WARN", f"no database yet at {os.path.basename(dbp)}", "it appears after the first live.py / collector.py / whoop_api.py sync run")
    else:
        c = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        def count(t):
            try:
                return c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                return None
        hr, raw, rec = count("ble_hr"), count("ble_raw"), count("api_recovery")
        say("PASS" if hr else "WARN", f"strap heart-rate samples: {hr or 0}", "" if hr else ".venv/bin/python live.py  (wear the strap)")
        say("PASS" if raw else "WARN", f"raw strap frames drained: {raw or 0}", "" if raw else ".venv/bin/python collector.py --live 20")
        say("PASS" if rec else "INFO", f"WHOOP API recovery scores: {rec or 0}", "" if rec else "optional: whoop_api.py login + sync (needs a WHOOP account)")
        last = c.execute("SELECT MAX(ts) FROM ble_hr").fetchone()[0] if hr else None
        if last:
            say("INFO", f"last strap sample {int((time.time() - last) / 60)} min ago")
        logs = count("device_logs")
        say("PASS" if logs else "INFO", f"decoded firmware log lines: {logs or 0}", "" if logs else ".venv/bin/python decode.py")

    # 5. Dashboard + background services
    s = socket.socket(); s.settimeout(0.5)
    up = s.connect_ex(("127.0.0.1", int(os.environ.get("PORT", 8787)))) == 0
    s.close()
    say("PASS" if up else "INFO", "dashboard answering on localhost:8787" if up else "dashboard not running",
        "" if up else ".venv/bin/python dashboard.py  then open http://localhost:8787")
    if sys.platform == "darwin" and shutil.which("launchctl"):
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        for label in ("com.whoop.collector", "com.whoop.dashboard"):
            say("PASS" if label in out else "INFO", f"launchd {label} {'loaded' if label in out else 'not loaded'}",
                "" if label in out else "optional: see whoop-tools/README.md, 'Always-on services'")

    fails, warns = results.count("FAIL"), results.count("WARN")
    print(f"\n{fails} fail, {warns} warn. " + ("Fix FAIL lines first." if fails else "Nothing blocking." if not warns else "WARN lines are next steps, not errors."))


if __name__ == "__main__":
    main()
