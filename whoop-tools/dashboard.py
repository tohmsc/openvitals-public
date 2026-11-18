#!/usr/bin/env python3
"""Localhost dashboard for your WHOOP data. Run: python dashboard.py -> http://localhost:8787

The front end lives in static/ (index.html, app.css, app.js, vendored Chart.js) and
works fully offline. This file only serves it plus two JSON endpoints.
"""
import json, time, os
from flask import Flask, jsonify, send_from_directory
import db

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
app = Flask(__name__, static_folder=STATIC, static_url_path="/static")

HISTORY = 400  # ~13 months; the client filters by range


def rows(c, q, *a):
    return [dict(r) for r in c.execute(q, a).fetchall()]


def _has(c, t):
    return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _clean(s):
    return (s or "").replace("\x00", "").strip()


@app.route("/api/summary")
def summary():
    c = db.connect()
    workouts = rows(c, "SELECT start,end,sport_name,strain,avg_hr,max_hr,kilojoule,distance_m,zone_raw "
                       "FROM api_workout ORDER BY start DESC LIMIT ?", HISTORY)
    for w in workouts:
        try:
            w["zones"] = json.loads(w.pop("zone_raw") or "null")
        except ValueError:
            w["zones"] = None
    profile = {r["key"]: r["value"] for r in c.execute("SELECT key,value FROM api_profile")}
    logs = []
    if _has(c, "device_logs"):
        logs = rows(c, "SELECT rtc,subsystem,message FROM device_logs ORDER BY rtc DESC LIMIT 60")
        for l in logs:
            l["message"] = _clean(l["message"])
    last_ble = db.get_state(c, "last_ble_sync")
    return jsonify({
        "profile": {"first_name": profile.get("first_name")},
        "recovery": rows(c, "SELECT created_at,recovery_score,hrv_rmssd_ms,resting_hr,spo2,skin_temp_c "
                             "FROM api_recovery WHERE recovery_score IS NOT NULL "
                             "ORDER BY created_at DESC LIMIT ?", HISTORY),
        "sleep": rows(c, "SELECT start,end,sleep_perf,sleep_efficiency,sleep_consistency,resp_rate,"
                         "total_in_bed_ms,total_rem_ms,total_sws_ms,total_light_ms,total_awake_ms,disturbances "
                         "FROM api_sleep WHERE COALESCE(nap,0)=0 AND total_in_bed_ms IS NOT NULL "
                         "ORDER BY start DESC LIMIT ?", HISTORY),
        "cycle": rows(c, "SELECT start,end,strain,avg_hr,max_hr,kilojoule FROM api_cycle "
                         "WHERE strain IS NOT NULL ORDER BY start DESC LIMIT ?", HISTORY),
        "workout": workouts,
        "live_hr": rows(c, "SELECT ts,hr FROM ble_hr ORDER BY ts DESC LIMIT 600"),
        "soc": rows(c, "SELECT rtc,soc_pct FROM device_soc ORDER BY rtc") if _has(c, "device_soc") else [],
        "logs": logs,
        "sync": {"ble": float(last_ble) if last_ble else None, "api": db.get_state(c, "api_since")},
        "counts": {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                   for t in ("api_recovery", "api_sleep", "api_cycle", "api_workout", "ble_hr", "ble_raw")},
    })


@app.route("/api/live")
def live():
    c = db.connect()
    r = c.execute("SELECT value FROM sync_state WHERE key='live_hr_now'").fetchone()
    cur = json.loads(r["value"]) if r else {}
    fresh = cur.get("hr") is not None and (time.time() - cur.get("ts", 0) < 8)
    recent = rows(c, "SELECT ts,hr FROM ble_hr ORDER BY ts DESC LIMIT 120")
    return jsonify({"live": fresh, "hr": cur.get("hr"), "rr_ms": cur.get("rr_ms", []),
                    "ts": cur.get("ts"),
                    "age": round(time.time() - cur.get("ts", 0), 1) if cur.get("ts") else None,
                    "recent": list(reversed(recent))})


@app.route("/static/chart.js")
def chartjs():  # kept for any old bookmarks/pages
    return send_from_directory(STATIC, "chart.umd.js", mimetype="application/javascript")


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8787))
    print(f"Dashboard: http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
