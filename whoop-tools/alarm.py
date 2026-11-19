#!/usr/bin/env python3
"""Silent alarm and buzz requests for the strap.

The alarm lives in two places:
  sync_state 'alarm'        what you asked for: {"enabled", "time": "07:00", "daily", "epoch"}
  sync_state 'alarm_armed'  what the strap holds: {"epoch", "ok", "ts", "checked"}

The WHOOP 4.0 has its own one-shot alarm (SET_ALARM_TIME). Once armed it rings at that time
by itself at that time (verified on a real strap, see README "Silent alarm"). sync() keeps
the strap's alarm equal to the next time you asked for; for a daily alarm that means
arming tomorrow's time once today's has rung. If arming fails (old firmware, no answer),
the daemon rings the strap itself at the time, if it is connected then (ring_due()).

Only one program can hold the Bluetooth link. While the daemon holds it, other programs
(dashboard, CLI) add a row to strap_cmd and the daemon runs it within about a second.

CLI (also reachable as `python whoop.py buzz|alarm ...`):
  python alarm.py buzz [pattern] [loops]
  python alarm.py alarm 07:00 [once]    # daily unless 'once'
  python alarm.py alarm off | show
"""
import asyncio, datetime as dt, json, sys, time
import db, whoop

KEY, ARMED, LAST = "alarm", "alarm_armed", "alarm_last_event"
BUZZ_TTL = 60         # a buzz request nobody ran within this long is dropped, not run late
RECHECK = 1800        # read the strap's alarm back this often, in case it lost it (reboot)
RING_LATE = 120       # the daemon's own ring (fallback) still runs this long after the time


# ---------------- what you asked for ----------------
def parse_hhmm(s):
    try:
        h, m = (int(x) for x in str(s).strip().split(":"))
    except ValueError:
        raise ValueError("time must look like 07:00") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("time must be between 00:00 and 23:59")
    return h, m


def next_time(h, m, now=None):
    """Unix time of the next local h:m strictly after `now`."""
    now = time.time() if now is None else now
    d = dt.datetime.fromtimestamp(now).replace(hour=h, minute=m, second=0, microsecond=0)
    if d.timestamp() <= now:
        d = (d + dt.timedelta(days=1)).replace(hour=h, minute=m)    # DST-safe: rebuild the time
    return int(d.timestamp())


def get(c):
    try:
        return json.loads(db.get_state(c, KEY) or "null") or {"enabled": False}
    except ValueError:
        return {"enabled": False}


def set_alarm(c, hhmm, daily=True, now=None):
    h, m = parse_hhmm(hhmm)
    cfg = {"enabled": True, "time": f"{h:02d}:{m:02d}", "daily": bool(daily)}
    if not daily:
        cfg["epoch"] = next_time(h, m, now)
    db.set_state(c, KEY, json.dumps(cfg))
    request(c, "alarm")                       # the daemon applies it on its next tick
    return cfg


def turn_off(c):
    cfg = dict(get(c), enabled=False)
    db.set_state(c, KEY, json.dumps(cfg))
    request(c, "alarm")
    return cfg


def desired(cfg, now=None):
    """Unix time the strap should ring next, or None."""
    now = time.time() if now is None else now
    if not cfg.get("enabled") or not cfg.get("time"):
        return None
    if not cfg.get("daily"):
        e = cfg.get("epoch")
        return e if e and e > now else None
    return next_time(*parse_hhmm(cfg["time"]), now)


def armed(c):
    try:
        return json.loads(db.get_state(c, ARMED) or "null") or {}
    except ValueError:
        return {}


def status(c, now=None):
    """Everything the dashboard and CLI show about the alarm."""
    now = time.time() if now is None else now
    cfg, a = get(c), armed(c)
    nxt = desired(cfg, now)
    try:
        last = json.loads(db.get_state(c, LAST) or "null")
    except ValueError:
        last = None
    return {
        "enabled": bool(cfg.get("enabled")) and nxt is not None,
        "time": cfg.get("time"), "daily": bool(cfg.get("daily")),
        "next": nxt,
        # on the strap = it will ring on its own at `next`, even if this Mac is off
        "on_strap": bool(nxt and a.get("epoch") == nxt and a.get("ok")),
        "armed": a or None,
        "last_event": last,
    }


# ---------------- requests for the daemon ----------------
def request(c, kind, arg=None):
    cur = c.execute("INSERT INTO strap_cmd(ts,kind,arg) VALUES(?,?,?)",
                    (time.time(), kind, json.dumps(arg) if arg is not None else None))
    c.commit()
    return cur.lastrowid


def result(c, rid):
    r = c.execute("SELECT * FROM strap_cmd WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def wait(c, rid, timeout=15.0):
    """Wait for the daemon to run request `rid`. Returns its row (status may still be pending)."""
    end = time.time() + timeout
    while True:
        r = result(c, rid)
        if not r or r["status"] != "pending" or time.time() > end:
            return r
        time.sleep(0.3)


def _finish(c, rid, status, res):
    c.execute("UPDATE strap_cmd SET status=?, result=?, done_ts=? WHERE id=?",
              (status, json.dumps(res), time.time(), rid))
    c.commit()


async def run_requests(w, c, now=None):
    """Run pending strap_cmd rows on a connected strap. Returns True if the alarm needs a sync."""
    now = time.time() if now is None else now
    poke = False
    for r in c.execute("SELECT * FROM strap_cmd WHERE status='pending' ORDER BY id").fetchall():
        kind = r["kind"]
        if kind == "alarm":                         # "the alarm setting changed"
            poke = True
            _finish(c, r["id"], "done", {"note": "alarm sync"})
            continue
        if kind in ("buzz", "ring") and now - r["ts"] > BUZZ_TTL:
            _finish(c, r["id"], "expired", {"note": "strap was not connected in time"})
            continue
        try:
            arg = json.loads(r["arg"]) if r["arg"] else {}
            if kind == "buzz":
                resp = await w.buzz(int(arg.get("pattern", 2)), int(arg.get("loops", 1)))
            elif kind == "stop":
                resp = await w.stop_buzz()
            elif kind == "ring":
                resp = await w.run_alarm()
            else:
                _finish(c, r["id"], "failed", {"note": f"unknown request {kind!r}"})
                continue
        except Exception as ex:                     # a bad link: leave it for the reconnect
            _finish(c, r["id"], "failed", {"note": str(ex) or type(ex).__name__})
            raise
        # The strap answers RUN_HAPTICS_PATTERN with status 2 and still buzzes (seen on a
        # real 4.0), so "it answered" counts as done; HAPTICS_FIRED (event 60) confirms.
        _finish(c, r["id"], "done" if resp else "failed",
                {"status": resp["status"]} if resp else {"note": "no answer from the strap"})
    return poke


def due_for_sync(c, now=None):
    """True if the strap's alarm differs from what was asked, or is due a read-back."""
    now = time.time() if now is None else now
    want, a = desired(get(c), now), armed(c)
    have = a.get("epoch") if (a.get("epoch") or 0) > now else None
    if want != have or (want is not None and not a.get("ok")):
        return True
    return want is not None and now - float(a.get("checked_ts") or 0) > RECHECK


async def sync(w, c, now=None):
    """Make the strap's alarm match what you asked for. Returns a short log line or None."""
    now = time.time() if now is None else now
    want, a = desired(get(c), now), armed(c)
    have = a.get("epoch") if (a.get("epoch") or 0) > now else None
    if want is None:
        if have is None:
            return None
        r = await w.disable_alarm()
        ok = bool(r and r["ok"])
        # if clearing failed, keep the old time so it is tried again (never rung by us: 'clearing')
        db.set_state(c, ARMED, json.dumps({"epoch": None, "ok": True, "ts": now} if ok else
                                          dict(a, ok=False, clearing=True, ts=now)))
        return "alarm cleared on the strap" if ok else "could not clear the strap alarm"
    if want == have and a.get("ok"):              # due a read-back only
        try:
            back = await w.get_alarm()
        except TimeoutError:
            return None
        if back == want:
            db.set_state(c, ARMED, json.dumps(dict(a, checked_ts=now)))
            return None
    r = await w.set_alarm(want)
    try:
        back = await w.get_alarm()
    except TimeoutError:
        back = None
    ok = bool(r and r["ok"]) and back == want
    db.set_state(c, ARMED, json.dumps({"epoch": want, "ok": ok, "ts": now, "checked_ts": now,
                                       "readback": back}))
    when = dt.datetime.fromtimestamp(want).strftime("%a %H:%M")
    return f"alarm set on the strap for {when}" if ok else f"strap did not confirm the alarm for {when}"


def ring_due(c, now=None):
    """Fallback: the strap did not confirm its alarm and the time has come. The daemon then
    rings the strap itself (only possible while it is connected)."""
    now = time.time() if now is None else now
    a = armed(c)
    e = a.get("epoch")
    return bool(e and not a.get("ok") and not a.get("rang") and not a.get("clearing")
                and e <= now < e + RING_LATE)


def mark_rang(c, now=None):
    db.set_state(c, ARMED, json.dumps(dict(armed(c), rang=time.time() if now is None else now)))


def note_event(c, eid, now=None):
    """Remember the last alarm/haptics event the strap reported (shown on the dashboard)."""
    if eid in whoop.EVENT_NAMES:
        db.set_state(c, LAST, json.dumps({"id": eid, "name": whoop.EVENT_NAMES[eid],
                                          "ts": time.time() if now is None else now}))


# ---------------- CLI ----------------
def _fmt(e):
    return dt.datetime.fromtimestamp(e).strftime("%a %d %b %H:%M") if e else "-"


def describe(s):
    if not s["enabled"]:
        return "Alarm: off"
    how = "every day" if s["daily"] else "once"
    where = ("set on the strap, rings even if the Mac is off" if s["on_strap"]
             else "not on the strap yet (waiting for the strap to connect)")
    return f"Alarm: {s['time']} {how}, next {_fmt(s['next'])} - {where}"


async def _direct(c, what, arg=None):
    """No daemon running: connect to the strap ourselves."""
    import collector
    addr = collector.env_addr() or await whoop.find()
    if not addr:
        print("WHOOP not found (is it nearby and not connected to the phone?)")
        return 2
    async with whoop.Whoop(addr) as w:
        w.verbose = False
        w.on_event = lambda eid, inner: note_event(c, eid)
        await w.init()
        await asyncio.sleep(1.0)
        if what == "buzz":
            r = await w.buzz(arg["pattern"], arg["loops"])
            print("buzz sent" if r else "the strap did not answer")
            await asyncio.sleep(2.0)                     # let HAPTICS_FIRED arrive
        else:
            print(await sync(w, c) or "strap alarm already up to date")
    return 0


def cli(argv):
    import collector
    c = db.connect()
    mode = argv[0] if argv else "alarm"
    rest = argv[1:]
    daemon = collector.lock_holder()
    if mode == "buzz":
        arg = {"pattern": int(rest[0]) if rest else 2, "loops": int(rest[1]) if len(rest) > 1 else 1}
        whoop.haptics_payload(arg["pattern"], arg["loops"])            # validate early
        if not daemon:
            return asyncio.run(_direct(c, "buzz", arg))
        r = wait(c, request(c, "buzz", arg))
        print({"done": "buzz sent", "pending": "the daemon is not connected to the strap yet",
               "expired": "the strap was not connected"}.get(r["status"], f"buzz {r['status']}: {r['result']}"))
        return 0 if r["status"] == "done" else 1
    if mode != "alarm":
        print(__doc__)
        return 2
    sub = rest[0] if rest else "show"
    if sub == "off":
        turn_off(c)
    elif sub != "show":
        set_alarm(c, sub, daily=not (len(rest) > 1 and rest[1] == "once"))
    if sub != "show":
        if daemon:                                       # set_alarm/turn_off queued a request
            rid = c.execute("SELECT MAX(id) FROM strap_cmd WHERE kind='alarm'").fetchone()[0]
            wait(c, rid, timeout=10)
            end = time.time() + 8                        # the sync itself runs right after
            while due_for_sync(c) and time.time() < end:
                time.sleep(0.3)
        else:
            asyncio.run(_direct(c, "alarm"))
    print(describe(status(c)))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
