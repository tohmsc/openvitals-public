#!/usr/bin/env python3
"""Your strap on Telegram: a short morning message, nudges when something needs you,
and a couple of commands. Built from what the strap recorded.

Every morning (after TELEGRAM_DAILY_HOUR, default 08:00 local; on wake if the laptop slept):

    🔴 Looks like you're strained. Take it lighter today. Yesterday was a big day, so that tracks.

    • Night: resting HR 57, 8 above your usual. HRV 52 ms, lower than usual.
    • Yesterday was a big day: 1h 20m with your heart rate up, peak 158.
    • Strap battery 18%, charge it today.

The call (🟢 push / 🟡 moderate / 🔴 lighter) compares last night's resting heart rate and
HRV with your usual, as learned from your own strap nights (learn.py). It is a rule of thumb
for how hard to push, not medical advice.

After a night with the laptop asleep, the message waits (up to 45 minutes) until the strap
has handed over the night, instead of reporting "no data" a minute after you open the lid.

Nudges, sent once when they start (never between 22:00 and the morning hour):
  battery under 20% · strap charged again · strap not seen for 6 hours · strap back ·
  strap clock reset (it stops storing history) · cloud archive stuck for days ·
  health alerts (alerts.py: resting HR up, HRV down, short sleep, breathing rate up, very low
  recovery, each 3 clean nights in a row against your usual; once per episode)

Commands you can send the bot:  /status  (is everything running)  ·  /now  (today so far)
  /good · /ok · /rough  (how you feel today: the calls are tuned to your answers, see learn.py)
  /learned  (what it knows about you so far)

Setup:
  1. Create a bot with @BotFather, put its token in .env:  TELEGRAM_BOT_TOKEN=123:abc...
  2. python telegram_report.py pair      # prints a code and a link; open the link, press Start
     The chat that sends the code becomes the only recipient (stored as TELEGRAM_CHAT_ID).
  3. Nothing else: the daemon does the rest.

Run:  python telegram_report.py              # send the morning message now
      python telegram_report.py --today      # send today so far
      python telegram_report.py --print      # show the text, send nothing
      python telegram_report.py status       # show the /status text

Privacy: this sends a health summary to Telegram's servers (bot chats are not end-to-end
encrypted). Only these short messages are sent, never raw data.
"""
import json, os, secrets, sys, time
import requests
import alerts, config, db, learn, metrics, sleep
from metrics import day_stats, night_stats, day_bounds, battery      # noqa: F401 (also used by tests)

HERE = config.HERE
ENV = config.ENV
API = "https://api.telegram.org/bot{}/{}"
MIN_TS = 1_600_000_000                  # a strap clock earlier than this was never set
REPORT_GRACE = 45 * 60                  # how long the morning message waits for last night's data
WAKE_GAP = 300                          # no tick for this long = the laptop was asleep

# When this process last started or the laptop last woke, and the last time run_due ran.
# (The daemon imports us once; while the laptop sleeps nothing ticks.)
_awake = {"since": time.time(), "tick": time.time()}


def env():
    return config.env(ENV)


def configured():
    return bool(env().get("TELEGRAM_BOT_TOKEN"))


def call(token, method, **params):
    try:
        r = requests.post(API.format(token, method), json=params, timeout=30)
        j = r.json()
    except Exception as ex:
        # requests puts the URL, and so the bot token, in its messages: keep it out of logs
        raise RuntimeError(f"Telegram {method}: {str(ex).replace(token, '<token>')}") from None
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j.get('description')}")
    return j["result"]


def call_for(night, usual, thresholds=None, model=None):
    """green / yellow / red from last night against your usual (see learn.py)."""
    return learn.call_for(night, usual, thresholds, model)


def _dur(s):
    h, m = int(s // 3600), int(s % 3600 // 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


# ---------------- the morning message ----------------
SAY = {   # a few ways to say each call, rotated by day so it doesn't read like a form
    "green": ["You're well recovered. Good day to push.",
              "Looks like a strong day. Go for it.",
              "Body's ready. Make it count today."],
    "yellow": ["You're a bit off your best. Keep it moderate today.",
               "Not fully recharged. Steady effort, nothing maximal.",
               "Middling recovery. Train if you like, but leave something in the tank."],
    "red": ["Looks like you're strained. Take it lighter today.",
            "Your body's working hard. Easy day and an early night.",
            "You look run down. Go gentle: walk, eat well, skip the hard session."],
    "none": ["No read on you today, I didn't get last night's data. Wear the strap to bed and I'll have one tomorrow."],
    "new": ["Still learning your normal. A few more nights and I can tell you how recovered you are."],
    "suspect": ["No read on you today: last night's numbers don't look like a real measurement."],
}
DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴", "none": "⚪", "new": "⚪", "suspect": "⚪"}


def day_kind(day):
    if not day:
        return None
    if day["high_s"] >= 30 * 60 or day["max"] >= 150:
        return "big"
    if day["max"] < 105 and day["recorded_s"] >= 6 * 3600:
        return "calm"
    return "normal"


def compose(c, start, end, label, morning):
    """The morning message: one line on how to treat yourself, then a few bullets."""
    today = time.strftime("%Y-%m-%d", time.localtime(morning))
    up_from = learn.high_hr(c, today)
    night, day = night_stats(c, morning, learn.night_window(c)), day_stats(c, start, end, up_from)
    usual, kind, model = learn.usual(c, today), day_kind(day), learn.model(c)
    # zones as learned from feedback on earlier days: today's own answer must not flip today's call
    zones = learn.thresholds(c, before=today)
    state = call_for(night, usual, zones, model)
    # remember this morning (learn.py learns from these rows), with how hard the day before was
    before = day if label == "Yesterday" else day_stats(c, morning - 86400, morning, up_from)
    est = learn.remember(c, today, night, before, usual, zones, model)
    head = SAY[state][time.localtime(morning).tm_yday % len(SAY[state])]
    if kind == "big" and label == "Yesterday":
        if state in ("red", "yellow"):
            head += " Yesterday was a big day, so that tracks."
        elif state == "green":
            head += " You bounced back well from yesterday."
    bullets = []
    then = usual.get("then")
    if morning:
        try:
            slept = sleep.night(c, morning)
        except Exception as ex:                       # the message goes out without it
            print("sleep error:", ex)
            slept = None
        if slept:
            bullets.append(f"{slept['headline']} {slept['detail']}")
    if night and night.get("suspect"):
        bullets.append(f"Night: the strap reported a resting HR of {night['resting']:.0f}, which isn't believable. "
                       "Check it sits snug, a finger above the wrist bone.")
    elif night:
        line = f"Night: resting HR {night['resting']:.0f}"
        if usual.get("rhr"):
            d = night["resting"] - usual["rhr"]
            line += (", right at your usual" if abs(d) <= 2 else
                     f", {d:.0f} above your usual" if d > 0 else f", {-d:.0f} below your usual")
        elif then:                      # no usual yet: show the WHOOP-era number, without judging against it
            line += f" ({then['rhr']:.0f} on WHOOP in {then['year']})"
        line += "."
        if night["rmssd"]:
            line += f" HRV {night['rmssd']:.0f} ms"
            if usual.get("hrv"):
                ratio = night["rmssd"] / usual["hrv"]
                line += ", lower than usual" if ratio < 0.9 else ", higher than usual" if ratio > 1.1 else ", normal for you"
            line += "."
        if est:
            line += f" Recovery about {est[0]:.0f}%" + ("." if est[1] == "hrv+rhr" else " (from resting HR only, no clean HRV reading).")
        bullets.append(line)
        run = learn.trend(c, today, usual)
        if run:
            bullets.append(f"Resting HR has been up {run} mornings in a row. That pattern usually means you're "
                           "run down or fighting something off: keep it easy until it comes back down.")
    when = "Yesterday" if label == "Yesterday" else "Today so far"
    if not day:
        bullets.append("Nothing recorded yesterday." if label == "Yesterday" else "Nothing recorded yet today.")
    else:
        short = f" Only {_dur(day['recorded_s'])} recorded." if label == "Yesterday" and day["recorded_s"] < 8 * 3600 else ""
        if label != "Yesterday":
            bullets.append(f"Today so far: avg HR {day['avg']:.0f}, peak {day['max']}, {_dur(day['recorded_s'])} recorded.")
        elif kind == "big":
            bullets.append(f"{when} was a big day: {_dur(day['high_s'])} with your heart rate up, peak {day['max']}.{short}")
        elif kind == "calm":
            bullets.append(f"{when} was a calm one: avg HR {day['avg']:.0f}, never above {day['max']}.{short}")
        else:
            bullets.append(f"{when} was a normal day: avg HR {day['avg']:.0f}, peak {day['max']}.{short}")
    if label == "Yesterday":
        try:
            held = held_back_line(c, morning)
        except Exception as ex:                       # the message goes out without it
            print("coverage error:", ex)
            held = None
        if held:
            bullets.append(held)
    soc = battery(c)
    if soc is not None and soc < 30:
        bullets.append(f"Strap battery {soc:.0f}%, charge it today.")
    return f"{DOT[state]} {head}\n\n" + "\n".join("• " + b for b in bullets)


def held_back_line(c, morning, now=None):
    """If yesterday's strain estimate was held back for too little heart rate: how much the
    strap recorded and where the biggest hole was, so it's clear what to change.
    Uses scores.py's own day window and coverage rule, on the data as it is now."""
    import scores
    day = time.strftime("%Y-%m-%d", time.localtime(morning - 43200))
    if not scores._has(c, "estimates"):
        return None
    est = scores._get(c, day, "strain", scores.current(c))
    if not est or est["ok"]:
        return None
    a, b, ongoing = scores.day_window(c, day, now)
    if ongoing or b <= a:
        return None
    share = scores.heart_zones(c, a, b, 50, 200)["recorded_s"] / (b - a)
    need = scores.GATES["strain_coverage"]
    if share >= need:
        return None
    line = f"Strain held back: the strap recorded {share:.0%} of yesterday (needs {need:.0%})."
    cov = metrics.coverage(c, a, b, now)
    if cov["gaps"]:
        line += f" Biggest gap {metrics.gap_text(cov['gaps'][0])}."
    if cov["pending_s"] >= 600 and not (cov["gaps"] and cov["gaps"][0]["pending_s"] >= 600):
        line += " Part of it is still on the strap and comes in at the next sync."
    return line


# ---------------- status + nudges ----------------
def _ago(s):
    return "just now" if s < 90 else f"{int(s // 60)} min ago" if s < 5400 else f"{s / 3600:.0f} h ago"


def last_seen(c):
    """Seconds since the strap last delivered anything (live or stored), or None."""
    live = c.execute("SELECT MAX(ts) m FROM ble_hr").fetchone()["m"] or 0
    sync = float(db.get_state(c, "last_ble_sync", 0) or 0)
    seen = max(live, sync)
    return time.time() - seen if seen else None


def raw_backlog_hours(c):
    r = c.execute("SELECT MIN(ts) a FROM ble_raw").fetchone()
    return (time.time() - r["a"]) / 3600 if r["a"] else 0


def status_text(c, e=None):
    """Answer to /status: is everything running."""
    e = e if e is not None else env()
    lines = []
    try:
        live = json.loads(db.get_state(c, "live_hr_now", "{}") or "{}")
    except ValueError:
        live = {}
    age = last_seen(c)
    if live.get("hr") and time.time() - live.get("ts", 0) < 20:
        lines.append(f"Strap connected, heart rate {live['hr']} right now.")
    elif live.get("hr") == 0 and time.time() - live.get("ts", 0) < 20:
        lines.append("Strap connected, but it's not on your wrist.")
    elif age is None:
        lines.append("Strap never seen yet.")
    else:
        lines.append(f"Strap not connected. Last seen {_ago(age)}.")
    soc = battery(c)
    if soc is not None:
        lines.append(f"Battery {soc:.0f}%" + (", charge it today." if soc < 20 else "."))
    today = day_stats(c, *day_bounds(0))
    lines.append(f"Recorded today: {_dur(today['recorded_s'])}." if today else "Nothing recorded today yet.")
    clk = c.execute("SELECT rtc FROM clock_sync ORDER BY ts DESC LIMIT 1").fetchone()
    if clk and clk["rtc"] < MIN_TS:
        lines.append("Strap clock isn't set (I set it on every connect and it didn't take), so stored history "
                     "can't be placed in time.")
    if e.get("CF_R2_BUCKET"):
        keep = float(e.get("CF_R2_KEEP_HOURS") or 24)
        late = raw_backlog_hours(c) - keep
        n = c.execute("SELECT COUNT(*) n FROM raw_archive").fetchone()["n"]
        lines.append(f"Cloud archive {late / 24:.0f} days behind, check the Cloudflare login." if late > 72
                     else f"Cloud archive up to date ({n} hours stored).")
    return "\n".join("• " + l for l in lines)


def nudges(c, e, uptime):
    """(key, should it be on, message when it starts, message when it ends or None)."""
    out = []
    soc = battery(c)
    if soc is not None:
        on = db.get_state(c, "telegram_alert_battery") == "1"
        # on below 20%; once on, stays on until the strap is properly charged again
        out.append(("battery", soc < 20 or (on and soc < 90),
                    f"🔋 Strap battery is at {soc:.0f}%. Charge it today so it keeps recording.",
                    f"🔋 Strap's charged ({soc:.0f}%). Good to go."))
    age = last_seen(c)
    if age is not None and uptime > 600:            # give a freshly started daemon time to connect
        on = db.get_state(c, "telegram_alert_strap") == "1"
        out.append(("strap", age > 6 * 3600 or (on and age > 600),
                    f"📡 I haven't seen your strap for {age / 3600:.0f} hours. It keeps recording on its own, "
                    "but bring it near the laptop so I can collect it.",
                    "📡 Strap's back. I've collected what it stored while it was away."))
    clk = c.execute("SELECT rtc FROM clock_sync ORDER BY ts DESC LIMIT 1").fetchone()
    if clk:
        out.append(("clock", clk["rtc"] < MIN_TS,
                    "⏱ Your strap's clock has reset and didn't take the time I set on connect, so what it "
                    "stores can't be placed in time. Put it on the charger for a minute to restart it; "
                    "if that doesn't help, open the WHOOP app once.", None))
    if e.get("CF_R2_BUCKET"):
        late = raw_backlog_hours(c) - float(e.get("CF_R2_KEEP_HOURS") or 24)
        out.append(("archive", late > 72,
                    f"☁️ The cloud archive is {late / 24:.0f} days behind. The Cloudflare login has probably "
                    "expired: run `npx wrangler login` on the laptop. Nothing is lost meanwhile.", None))
    # health alerts (alerts.py): sent once when an episode starts; quiet while it lasts and
    # when it ends. A new message only after it has ended and come back.
    for key, a in alerts.evaluate(c).items():
        out.append((f"health_{key}", a is not None, alerts.message(a) if a else None, None))
    return out


def send_nudges(c, token, chat, e, api, uptime):
    sent = 0
    for key, should, start_msg, end_msg in nudges(c, e, uptime):
        state = f"telegram_alert_{key}"
        on = db.get_state(c, state) == "1"
        if should and not on:
            api(token, "sendMessage", chat_id=chat, text=start_msg)
            db.set_state(c, state, "1"); sent += 1
        elif on and not should:
            if end_msg:
                api(token, "sendMessage", chat_id=chat, text=end_msg); sent += 1
            db.set_state(c, state, "0")
    return sent


# ---------------- pairing, commands, sending ----------------
def pair_code(c):
    code = db.get_state(c, "telegram_pair_code")
    if not code:
        code = f"{secrets.randbelow(900000) + 100000}"
        db.set_state(c, "telegram_pair_code", code)
    return code


def _updates(c, token, api):
    offset = int(db.get_state(c, "telegram_update_offset", 0) or 0)
    ups = api(token, "getUpdates", offset=offset, timeout=0)
    if ups:
        db.set_state(c, "telegram_update_offset", ups[-1]["update_id"] + 1)
    return ups


def try_pair(c, token, api=call):
    """Look for a message containing the pairing code; bind to that chat. Returns chat id or None."""
    code = pair_code(c)
    for u in _updates(c, token, api):
        msg = u.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if chat and code in (msg.get("text") or ""):
            with open(ENV, "a") as f:
                f.write(f"\nTELEGRAM_CHAT_ID={chat}\n")
            api(token, "sendMessage", chat_id=chat,
                text="Linked. I'll message you every morning, and nudge you if the strap needs charging "
                     "or goes missing. Send /status or /now any time. Here's today so far:")
            send_report(c, token, chat, 0, "Today so far", api)
            return chat
    return None


FEEL_WORDS = {"good": ("good", "great", "strong", "fresh", "👍"),
              "ok": ("ok", "okay", "fine", "meh", "so-so"),
              "rough": ("rough", "bad", "tired", "wrecked", "sick", "👎")}
ASK = "How do you feel today? /good · /ok · /rough"


def feel_word(text):
    for word, alts in FEEL_WORDS.items():
        if any(text == a or text.startswith(a + " ") for a in alts):
            return word
    return None


def feel_reply(c, word):
    """Record how you feel and say what that did to the calls."""
    _, called, before, after = learn.record_feel(c, word)
    text = f"Noted: you feel {word}."
    if called in ("green", "yellow", "red"):
        match = {"green": "good", "yellow": "ok", "red": "rough"}[called] == word
        text += f" I had you at {DOT[called]} this morning" + (", so we agree." if match else ".")
    drift = (after["green_min"] - before["green_min"]) + (after["red_max"] - before["red_max"])
    if drift < -0.01:                   # zones moved down: the same night reads better next time
        text += " I'll be a little less cautious with you from now on."
    elif drift > 0.01:
        text += " I'll be a little more cautious with you from now on."
    return text


def answer_commands(c, token, chat, e, api):
    """Reply to /status and /now from the linked chat. Anyone else is ignored."""
    n = 0
    for u in _updates(c, token, api):
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat):
            continue
        text = (msg.get("text") or "").strip().lower().lstrip("/").split("@")[0]
        feel = feel_word(text)
        if text.startswith("status"):
            api(token, "sendMessage", chat_id=chat, text=status_text(c, e))
        elif text.startswith("now") or text.startswith("today"):
            send_report(c, token, chat, 0, "Today so far", api)
        elif text.startswith("learn"):
            api(token, "sendMessage", chat_id=chat, text=learn.learned_text(c))
        elif feel:
            api(token, "sendMessage", chat_id=chat, text=feel_reply(c, feel))
        else:
            api(token, "sendMessage", chat_id=chat,
                text="I send one message each morning and nudge you when the strap needs attention.\n"
                     "/status: is everything running\n/now: today so far\n"
                     "/good /ok /rough: how you feel today (I learn from it)\n/learned: what I know about you")
        n += 1
    return n


def send_report(c, token, chat, offset_days, label, api=call, now=None, ask=False):
    start, end = day_bounds(offset_days, now)
    text = compose(c, start, end, label, day_bounds(0, now)[0])
    api(token, "sendMessage", chat_id=chat, text=text + (f"\n\n{ASK}" if ask else ""))


def night_is_in(c, now):
    """True once the strap's stored history has been read (and decoded) through the end of last
    night. After a night with the laptop asleep that takes a few minutes from waking: the strap
    reconnects, hands over what it stored, and decode.py turns it into heart rate."""
    seen = db.get_state(c, "history_ts")
    if seen is None:                    # history was never drained here (--no-drain): nothing to wait for
        return True
    return float(seen) >= day_bounds(0, now)[0] + learn.night_window(c)[1] * 3600


def report_ready(c, today, now):
    """Send the morning message now? Not before your night is over (learn.night_window: a late
    riser's night isn't done at 08:00); then yes once it is in, or after waiting REPORT_GRACE."""
    if now < day_bounds(0, now)[0] + learn.night_window(c)[1] * 3600:
        return False
    if night_is_in(c, now):
        return True
    day, _, since = (db.get_state(c, "telegram_report_waiting") or "").partition("|")
    if day != today:
        db.set_state(c, "telegram_report_waiting", f"{today}|{now}")
        return False
    return now - float(since) >= REPORT_GRACE


def awake_for():
    """Seconds since this process started or the laptop woke, whichever is later."""
    t = time.time()
    if t - _awake["tick"] > WAKE_GAP:
        _awake["since"] = t
    _awake["tick"] = t
    return t - _awake["since"]


def run_due(api=call, now=None, uptime=None):
    """Called by the daemon about twice a minute: pair, answer commands, nudge, morning message."""
    e = env()
    token = e.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    c = db.connect()
    up = awake_for() if uptime is None else uptime      # before anything can return early
    chat = e.get("TELEGRAM_CHAT_ID")
    if not chat:
        return "paired" if try_pair(c, token, api) else None
    did = "replied" if answer_commands(c, token, chat, e, api) else None
    now = now or time.time()
    lt = time.localtime(now)
    hour = int(e.get("TELEGRAM_DAILY_HOUR") or 8)
    if not hour <= lt.tm_hour < 22:                     # quiet hours: nothing unprompted at night
        return did
    if send_nudges(c, token, chat, e, api, up):
        did = "nudged"
    today = time.strftime("%Y-%m-%d", lt)
    if db.get_state(c, "telegram_last_report") != today and report_ready(c, today, now):
        send_report(c, token, chat, -1, "Yesterday", api, now, ask=True)
        db.set_state(c, "telegram_last_report", today)  # only after a successful send
        did = "sent"
    return did


if __name__ == "__main__":
    args = sys.argv[1:]
    e, c = env(), db.connect()
    token = e.get("TELEGRAM_BOT_TOKEN")
    offset, label = (0, "Today so far") if "--today" in args else (-1, "Yesterday")
    if "--print" in args:
        print(compose(c, *day_bounds(offset), label, day_bounds(0)[0]))
    elif args[:1] == ["status"]:
        print(status_text(c, e))
    elif not token:
        print("Put TELEGRAM_BOT_TOKEN in .env first (see the top of this file).")
    elif args[:1] == ["pair"] or not e.get("TELEGRAM_CHAT_ID"):
        if e.get("TELEGRAM_CHAT_ID"):
            print("Already linked to a chat. Remove TELEGRAM_CHAT_ID from .env to pair again.")
        else:
            bot = call(token, "getMe").get("username")
            print(f"Open https://t.me/{bot}?start={pair_code(c)} and press Start (or send {pair_code(c)} to @{bot}).")
            print("Linked now." if try_pair(c, token) else "Waiting: the daemon checks twice a minute, or run this again.")
    else:
        send_report(c, token, e["TELEGRAM_CHAT_ID"], offset, label)
        print("sent")
