#!/usr/bin/env python3
"""Localhost dashboard for your WHOOP data. Run: python dashboard.py -> http://localhost:8787

The front end lives in static/ (index.html, app.css, app.js, vendored Chart.js) and
works fully offline. This file only serves it plus two JSON endpoints.
"""
import json, time, os, sys, subprocess, threading
from flask import Flask, jsonify, request, send_from_directory
import alerts, db, learn, metrics, scores, sleep, stress

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
app = Flask(__name__, static_folder=STATIC, static_url_path="/static")

HISTORY = 400  # ~13 months; the client filters by range
# Folder holding collector.py + .env + .tokens.json (defaults to this one).
WHOOP_HOME = os.environ.get("WHOOP_HOME") or HERE
DEMO = "demo" in os.path.basename(db.DB_PATH)


def rows(c, q, *a):
    return [dict(r) for r in c.execute(q, a).fetchall()]


def _has(c, t):
    return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


def _safe(fn, c):
    """A new section failing must not take the whole summary down."""
    try:
        return fn(c)
    except Exception as ex:          # noqa: BLE001
        print(f"{fn.__module__}.{fn.__name__} error:", ex)
        return None


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
    # Newest first by arrival, not by rtc: rtc is the firmware's tick counter and restarts
    # when the strap reboots, so after a reboot the newest lines carry the smallest numbers.
    logs = []
    if _has(c, "device_logs"):
        logs = rows(c, "SELECT rtc,subsystem,message FROM device_logs ORDER BY rowid DESC LIMIT 60")
        for l in logs:
            l["message"] = _clean(l["message"])
    soc = []
    if _has(c, "device_soc"):
        db.add_columns(c, [("device_soc", "seen", "REAL")])      # a database decode.py hasn't touched since
        soc = rows(c, "SELECT rtc,soc_pct FROM device_soc ORDER BY COALESCE(seen, 0), rtc")
    last_ble = db.get_state(c, "last_ble_sync")
    # "Today" = strap data since local midnight. The WHOOP cloud tables are the archive.
    midnight, tomorrow = metrics.day_bounds(0)
    today_hr = rows(c, "SELECT ts,hr,rr_ms FROM ble_hr WHERE ts >= ? AND hr > 0 ORDER BY ts", midnight)
    stats = metrics.day_stats(c, midnight, tomorrow, learn.high_hr(c)) or {}
    arch = c.execute("SELECT MIN(created_at) a, MAX(created_at) b FROM api_recovery").fetchone()
    feel = c.execute("SELECT feel FROM daily_night WHERE date=?", (time.strftime("%Y-%m-%d"),)).fetchone()
    return jsonify({
        "today_hr": today_hr,
        # same numbers the Telegram message uses (metrics.py), so the two never disagree
        "today": {k: stats.get(k) for k in ("rmssd", "n_rr", "resting", "high_s", "recorded_s")},
        "battery": metrics.battery(c),
        "pack_battery": metrics.pack_battery(c),
        "personal": dict(learn.summary(c), text=learn.learned_text(c).replace("• ", "").split("\n"),
                         feel=learn.FEEL_WORD.get(feel["feel"]) if feel and feel["feel"] else None),
        "archive": {"start": arch["a"], "end": arch["b"]},
        "nights": sleep.recent(c, 30),               # how you slept (sleep.py), newest first
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
        # strap estimates of the same scores (scores.py), only the ones that passed their checks
        "estimates": scores.for_app(c),
        # stress monitor (stress.py): today's 5-minute series, the reading now, the baseline used
        "stress": _safe(stress.for_app, c),
        # health alerts that are on (alerts.py): sustained changes against your usual
        "alerts": _safe(alerts.active, c) or [],
        "live_hr": rows(c, "SELECT ts,hr FROM ble_hr ORDER BY ts DESC LIMIT 600"),
        "soc": soc,
        "coverage": metrics.coverage_days(c, 7),     # how much of each day has heart rate, and why not
        "logs": logs,
        "sync": {"ble": float(last_ble) if last_ble else None, "api": db.get_state(c, "api_since")},
        "demo": DEMO,
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


# ───────── Resync: run one collector cycle (strap drain + live HR + WHOOP API + decode + backup) ─────────
SYNC = {"running": False, "started": None, "finished": None, "ok": None, "log": []}
_sync_lock = threading.Lock()


def _run_sync():
    env = dict(os.environ, WHOOP_DB=db.DB_PATH, PYTHONUNBUFFERED="1")
    try:
        p = subprocess.Popen([sys.executable, "collector.py", "--live", "15"], cwd=WHOOP_HOME, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            line = line.strip()
            if line:
                SYNC["log"] = (SYNC["log"] + [line])[-40:]
        SYNC["ok"] = p.wait(timeout=600) == 0
    except Exception as ex:  # noqa: BLE001 - surface anything to the UI
        SYNC["log"] = SYNC["log"] + [f"error: {ex}"]
        SYNC["ok"] = False
    finally:
        SYNC["running"] = False
        SYNC["finished"] = time.time()


@app.route("/api/sync", methods=["GET", "POST"])
def resync():
    if request.method == "POST":
        # Custom header = browsers must preflight, so other sites can't trigger this.
        if request.headers.get("X-Vitals") != "1":
            return jsonify({"error": "missing header"}), 403
        if DEMO:
            return jsonify({"error": "Demo database: syncing is disabled so real data never lands in it."}), 409
        with _sync_lock:
            if SYNC["running"]:
                return jsonify(SYNC), 409
            SYNC.update(running=True, started=time.time(), finished=None, ok=None, log=[])
        threading.Thread(target=_run_sync, daemon=True).start()
    return jsonify(SYNC)


@app.route("/api/feel", methods=["POST"])
def feel():
    """Record how you feel today (good / ok / rough); learn.py tunes its calls to these answers."""
    if request.headers.get("X-Vitals") != "1":
        return jsonify({"error": "missing header"}), 403
    word = (request.get_json(silent=True) or {}).get("feel")
    if word not in learn.FEEL:
        return jsonify({"error": "feel must be good, ok or rough"}), 400
    if DEMO:
        return jsonify({"error": "Demo database: nothing is recorded."}), 409
    c = db.connect()
    learn.record_feel(c, word)
    return jsonify({"feel": word, "thresholds": learn.thresholds(c)})


@app.route("/api/alarm", methods=["GET", "POST"])
def alarm_api():
    """Silent alarm. GET: the alarm and whether the strap holds it.
    POST {"time": "07:00", "daily": true} sets it; {"off": true} turns it off.
    The daemon (which holds the strap) puts it on the strap within a few seconds."""
    import alarm, collector
    c = db.connect()
    if request.method == "POST":
        if request.headers.get("X-Vitals") != "1":
            return jsonify({"error": "missing header"}), 403
        if DEMO:
            return jsonify({"error": "Demo database: the alarm is disabled."}), 409
        body = request.get_json(silent=True) or {}
        try:
            if body.get("off"):
                alarm.turn_off(c)
            else:
                alarm.set_alarm(c, body.get("time", ""), daily=bool(body.get("daily", True)))
        except ValueError as ex:
            return jsonify({"error": str(ex)}), 400
        end = time.time() + 6                    # give the daemon a moment to put it on the strap
        while collector.lock_holder() and alarm.due_for_sync(c) and time.time() < end:
            time.sleep(0.3)
    return jsonify(dict(alarm.status(c), daemon=bool(collector.lock_holder())))


@app.route("/api/buzz", methods=["POST"])
def buzz_api():
    """Vibrate the strap once (a test). Runs through the daemon, which holds the strap."""
    import alarm, collector
    if request.headers.get("X-Vitals") != "1":
        return jsonify({"error": "missing header"}), 403
    if DEMO:
        return jsonify({"error": "Demo database: no strap."}), 409
    if not collector.lock_holder():
        return jsonify({"error": "The strap service is not running, so nothing can reach the strap."}), 409
    c = db.connect()
    r = alarm.wait(c, alarm.request(c, "buzz", {"pattern": 2, "loops": 1}), timeout=8)
    return jsonify({"status": r["status"]}), 200 if r["status"] == "done" else 504


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
