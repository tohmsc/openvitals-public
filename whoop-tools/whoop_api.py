#!/usr/bin/env python3
"""WHOOP v2 official API client: OAuth2 auth-code flow + incremental sync to SQLite.

First run:  python whoop_api.py login   (opens browser, stores refresh token locally)
Then:       python whoop_api.py sync     (pulls new sleep/recovery/cycle/workout)

Credentials come from .env; the refresh token is stored in .tokens.json (gitignored).
Nothing leaves your machine except calls to WHOOP's own servers.
"""
import os, json, time, sys, secrets, threading, webbrowser, urllib.parse, calendar
import http.server, socketserver, fcntl
import requests
import config, db

HERE = config.HERE
TOKENS = os.path.join(HERE, ".tokens.json")
AUTH_URL  = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API = "https://api.prod.whoop.com/developer"
SCOPES = "read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement offline"

env = config.env
OVERLAP_DAYS = 3          # re-read this much before the watermark on every sync

def save_tokens(t):
    t["obtained_at"] = time.time()
    # write-then-rename: a crash mid-write must not destroy the (rotating) refresh token
    tmp = TOKENS + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(t, f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, TOKENS)

def load_tokens():
    return json.load(open(TOKENS)) if os.path.exists(TOKENS) else None

# ---------------- OAuth ----------------
def login():
    e = env()
    cid, csec = e.get("WHOOP_CLIENT_ID"), e.get("WHOOP_CLIENT_SECRET")
    redirect = e.get("WHOOP_REDIRECT_URI", "http://localhost:8765/callback")
    if not cid or cid == "your_client_id_here":
        print("Fill WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET in whoop-tools/.env first."); return
    state = secrets.token_urlsafe(16)
    port = int(urllib.parse.urlparse(redirect).port or 8765)
    params = {"response_type":"code","client_id":cid,"redirect_uri":redirect,
              "scope":SCOPES,"state":state}
    auth = AUTH_URL + "?" + urllib.parse.urlencode(params)
    code_box = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in q:
                code_box["code"] = q["code"][0]; code_box["state"] = q.get("state",[None])[0]
                self.send_response(200); self.end_headers()
                self.wfile.write(b"<h2>WHOOP connected. You can close this tab.</h2>")
            else:
                self.send_response(400); self.end_headers()

    srv = socketserver.TCPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    print("Opening browser to authorize... if it doesn't open, visit:\n", auth)
    webbrowser.open(auth)
    for _ in range(300):
        if "code" in code_box: break
        time.sleep(1)
    srv.server_close()
    if "code" not in code_box:
        print("No authorization received (timed out)."); return
    if code_box.get("state") != state:
        print("State mismatch — aborting for safety."); return
    r = requests.post(TOKEN_URL, data={
        "grant_type":"authorization_code","code":code_box["code"],
        "redirect_uri":redirect,"client_id":cid,"client_secret":csec}, timeout=30)
    r.raise_for_status(); save_tokens(r.json())
    print("Logged in. Refresh token stored. Run: python whoop_api.py sync")

def access_token():
    # lock so two processes (launchd run + manual sync) can't both spend the
    # single-use refresh token; the loser re-reads the fresh tokens instead
    with open(TOKENS + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        t = load_tokens()
        if not t: raise RuntimeError("Not logged in. Run: python whoop_api.py login")
        if time.time() - t.get("obtained_at", 0) < t.get("expires_in", 3600) - 120:
            return t["access_token"]
        e = env()
        r = requests.post(TOKEN_URL, data={
            "grant_type":"refresh_token","refresh_token":t["refresh_token"],
            "client_id":e["WHOOP_CLIENT_ID"],"client_secret":e["WHOOP_CLIENT_SECRET"],
            "scope":"offline"}, timeout=30)
        r.raise_for_status(); nt = r.json()
        nt.setdefault("refresh_token", t["refresh_token"])
        save_tokens(nt); return nt["access_token"]

def get(path, params=None):
    h = {"Authorization": f"Bearer {access_token()}"}
    r = requests.get(API + path, headers=h, params=params, timeout=30)
    if r.status_code == 429:
        time.sleep(5); return get(path, params)
    r.raise_for_status(); return r.json()

def paged(path, since=None):
    params = {"limit": 25}
    if since: params["start"] = since
    while True:
        j = get(path, params)
        for rec in j.get("records", []): yield rec
        nt = j.get("next_token")
        if not nt: break
        params["nextToken"] = nt

# ---------------- sync ----------------
def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict): return default
        d = d.get(k)
    return d if d is not None else default

def _overlap(since):
    """The watermark moved back a few days. WHOOP scores records after it first lists them
    (a sleep appears PENDING, a day's strain keeps growing until the cycle closes), so the
    recent ones are read again; upserts make that harmless."""
    try:
        t = calendar.timegm(time.strptime(since[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return since
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t - OVERLAP_DAYS * 86400))


def sync():
    c = db.connect()
    newest = db.get_state(c, "api_since", "2020-01-01T00:00:00.000Z")
    since = _overlap(newest)
    counts = {}
    # Sleep
    for s in paged("/v2/activity/sleep", since):
        st = _g(s, "score", "stage_summary")
        db.upsert(c, "api_sleep", {
            "id": str(s["id"]), "start": s.get("start"), "end": s.get("end"),
            "nap": int(bool(s.get("nap"))),
            "resp_rate": _g(s, "score", "respiratory_rate"),
            "sleep_perf": _g(s, "score", "sleep_performance_percentage"),
            "sleep_consistency": _g(s, "score", "sleep_consistency_percentage"),
            "sleep_efficiency": _g(s, "score", "sleep_efficiency_percentage"),
            "total_in_bed_ms": _g(st, "total_in_bed_time_milli"),
            "total_awake_ms": _g(st, "total_awake_time_milli"),
            "total_light_ms": _g(st, "total_light_sleep_time_milli"),
            "total_sws_ms": _g(st, "total_slow_wave_sleep_time_milli"),
            "total_rem_ms": _g(st, "total_rem_sleep_time_milli"),
            "disturbances": _g(st, "disturbance_count"),
            "raw": json.dumps(s)})
        newest = max(newest, s.get("end") or newest); counts["sleep"]=counts.get("sleep",0)+1
    # Recovery
    for rv in paged("/v2/recovery", since):
        db.upsert(c, "api_recovery", {
            "cycle_id": rv.get("cycle_id"), "sleep_id": str(rv["sleep_id"]) if rv.get("sleep_id") is not None else None,
            "created_at": rv.get("created_at"), "score_state": rv.get("score_state"),
            "recovery_score": _g(rv, "score", "recovery_score"),
            "resting_hr": _g(rv, "score", "resting_heart_rate"),
            "hrv_rmssd_ms": _g(rv, "score", "hrv_rmssd_milli"),
            "spo2": _g(rv, "score", "spo2_percentage"),
            "skin_temp_c": _g(rv, "score", "skin_temp_celsius"),
            "raw": json.dumps(rv)})
        counts["recovery"]=counts.get("recovery",0)+1
    # Cycle
    for cy in paged("/v2/cycle", since):
        db.upsert(c, "api_cycle", {
            "id": cy.get("id"), "start": cy.get("start"), "end": cy.get("end"),
            "score_state": cy.get("score_state"),
            "strain": _g(cy, "score", "strain"),
            "avg_hr": _g(cy, "score", "average_heart_rate"),
            "max_hr": _g(cy, "score", "max_heart_rate"),
            "kilojoule": _g(cy, "score", "kilojoule"), "raw": json.dumps(cy)})
        counts["cycle"]=counts.get("cycle",0)+1
    # Workout
    for w in paged("/v2/activity/workout", since):
        db.upsert(c, "api_workout", {
            "id": str(w.get("id")), "start": w.get("start"), "end": w.get("end"),
            "sport_name": w.get("sport_name") or str(w.get("sport_id")),
            "score_state": w.get("score_state"),
            "strain": _g(w, "score", "strain"),
            "avg_hr": _g(w, "score", "average_heart_rate"),
            "max_hr": _g(w, "score", "max_heart_rate"),
            "kilojoule": _g(w, "score", "kilojoule"),
            "distance_m": _g(w, "score", "distance_meter"),
            "zone_raw": json.dumps(_g(w, "score", "zone_durations") or {}),
            "raw": json.dumps(w)})
        counts["workout"]=counts.get("workout",0)+1
    # Profile
    try:
        p = get("/v2/user/profile/basic")
        for k, v in p.items():
            db.upsert(c, "api_profile", {"key": k, "value": str(v)})
    except Exception as ex:
        print("profile:", ex)
    # Body measurements: WHOOP's max heart rate for you (learn.py uses it for "heart rate up")
    try:
        for k, v in get("/v2/user/measurement/body").items():
            db.upsert(c, "api_profile", {"key": k, "value": str(v)})
    except Exception as ex:
        print("body measurement:", ex)
    db.set_state(c, "api_since", newest); c.commit()
    print("API sync complete:", counts, "| watermark:", newest)

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sync"
    {"login": login, "sync": sync}.get(cmd, sync)()
