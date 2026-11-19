#!/usr/bin/env python3
"""A local MCP server: lets an AI assistant (Claude Desktop, Claude Code, ...) read your strap
data from whoop.db on this Mac. Nothing leaves the machine except what the assistant itself
sends to its model. It never talks to the strap and never writes your data; the `sql` tool runs
on a read-only connection.

Plain JSON-RPC over stdin/stdout (MCP stdio transport), no extra packages.

Add it to Claude Code:
    claude mcp add openvitals -- ~/whoop-os/whoop-tools/.venv/bin/python ~/whoop-os/whoop-tools/mcp_server.py
or to Claude Desktop's claude_desktop_config.json:
    "mcpServers": {"openvitals": {"command": "/Users/you/whoop-os/whoop-tools/.venv/bin/python",
                                  "args": ["/Users/you/whoop-os/whoop-tools/mcp_server.py"]}}
"""
import json, os, re, sqlite3, sys, time, traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

PROTOCOL = "2025-06-18"
MAX_ROWS = 500
NOTE = "Estimates from a consumer strap, compared with your own history. Not medical advice."

_c = None


def conn():
    """Normal connection: learn.py caches what it fits in sync_state (not your data)."""
    global _c
    if _c is None:
        _c = db.connect()
    return _c


def ro():
    c = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def _day(s):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - s))


# ---------------- tools ----------------
def t_readiness(date=None):
    import readiness
    return dict(readiness.evaluate(conn(), date), note=NOTE)


def t_today():
    import alerts, metrics
    c = conn()
    today = _day(0)
    est = rows(c.execute("SELECT date, kind, value, ok, why FROM estimates WHERE date >= ? ORDER BY date DESC, kind",
                         (_day(86400),))) if _has(c, "estimates") else []
    live = c.execute("SELECT value FROM sync_state WHERE key='live_hr_now'").fetchone()
    live = json.loads(live["value"]) if live else None
    if live:
        live["age_s"] = round(time.time() - live.get("ts", 0))
    out = {"date": today, "estimates": est, "live": live, "alerts": alerts.active(c, today), "note": NOTE}
    for k, fn in (("battery", "battery"), ("pack_battery", "pack_battery")):
        try:
            out[k] = getattr(metrics, fn)(c)
        except Exception:
            out[k] = None
    return out


def t_nights(days=14):
    c = conn()
    since = _day(int(days) * 86400)
    d = rows(c.execute("SELECT date, rhr, hrv, n_rr, score recovery_estimate, call, feel FROM daily_night "
                       "WHERE date >= ? ORDER BY date DESC", (since,)))
    s = {r["date"]: r for r in rows(c.execute("SELECT date, onset, wake, asleep_s, awake_s, wakeups, status, headline "
                                              "FROM sleep_night WHERE date >= ?", (since,)))} if _has(c, "sleep_night") else {}
    for r in d:
        r["sleep"] = s.pop(r["date"], None)
    d += [{"date": k, "sleep": v} for k, v in s.items()]
    return {"nights": sorted(d, key=lambda r: r["date"], reverse=True),
            "units": {"rhr": "bpm", "hrv": "ms RMSSD", "asleep_s": "seconds"}, "note": NOTE}


def t_scores(days=14, kind=None):
    c = conn()
    if not _has(c, "estimates"):
        return {"scores": []}
    q = "SELECT date, kind, version, value, ok, why, row FROM estimates WHERE date >= ?"
    args = [_day(int(days) * 86400)]
    if kind:
        q += " AND kind = ?"; args.append(kind)
    out = rows(c.execute(q + " ORDER BY date DESC, kind", args))
    for r in out:
        try:
            r["row"] = json.loads(r["row"] or "null")
        except ValueError:
            pass
    return {"scores": out, "note": "ok=0 means the estimate did not pass its checks; `why` says which. " + NOTE}


def t_heart_rate(hours=6, end=None, step_s=60):
    end = float(end) if end else time.time()
    start = end - float(hours) * 3600
    step = max(1, int(step_s))
    if (end - start) / step > 2000:
        step = int((end - start) / 2000) + 1
    r = rows(conn().execute(
        "SELECT CAST(ts / ? AS INTEGER) * ? t, ROUND(AVG(hr), 1) hr, MIN(hr) min, MAX(hr) max, COUNT(*) n "
        "FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0 GROUP BY 1 ORDER BY 1", (step, step, start, end)))
    return {"start": start, "end": end, "step_s": step, "points": r,
            "note": "t is Unix seconds at the start of each step; n = seconds with a reading."}


def t_schema():
    c = ro()
    out = {}
    for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        out[name] = [r["name"] for r in c.execute(f"PRAGMA table_info({name})")]
    return {"tables": out, "hint": "ble_raw holds raw strap frames (large); ble_hr is 1 Hz heart rate with "
            "beat intervals in ms; daily_night / sleep_night / estimates are per day."}


READ_ONLY = re.compile(r"^\s*(select|with)\b", re.I)


def t_sql(query):
    if not READ_ONLY.match(query or "") or ";" in query.strip().rstrip(";"):
        raise ValueError("only a single SELECT (or WITH ... SELECT) statement")
    c = ro()
    cur = c.execute(query)
    cols = [d[0] for d in cur.description or []]
    data = cur.fetchmany(MAX_ROWS + 1)
    return {"columns": cols, "rows": [list(r) for r in data[:MAX_ROWS]], "truncated": len(data) > MAX_ROWS}


def _has(c, t):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None


TOOLS = {
    "readiness": (t_readiness, "Today's readiness (Primed / Balanced / Strained / Run down / Learning) and the "
                  "signals behind it: HRV and resting HR against your usual, breathing rate, training load.",
                  {"date": {"type": "string", "description": "YYYY-MM-DD, default today"}}, []),
    "today": (t_today, "Right now: live heart rate, strap and pack battery, today's and yesterday's Strain / "
              "Sleep / Recovery estimates, and any health alerts that are on.", {}, []),
    "nights": (t_nights, "Recent nights: resting HR, HRV (RMSSD ms), recovery estimate, sleep times.",
               {"days": {"type": "integer", "description": "how many days back, default 14"}}, []),
    "scores": (t_scores, "Strain, Sleep and Recovery estimates per day, with the checks each passed or failed.",
               {"days": {"type": "integer", "description": "default 14"},
                "kind": {"type": "string", "enum": ["strain", "sleep", "recovery"]}}, []),
    "heart_rate": (t_heart_rate, "Heart rate over a time range, averaged per step (min/max/avg).",
                   {"hours": {"type": "number", "description": "length of the range, default 6"},
                    "end": {"type": "number", "description": "Unix seconds, default now"},
                    "step_s": {"type": "integer", "description": "seconds per point, default 60"}}, []),
    "schema": (t_schema, "Tables and columns in the local database, for use with `sql`.", {}, []),
    "sql": (t_sql, f"Run one read-only SELECT on the local database (at most {MAX_ROWS} rows).",
            {"query": {"type": "string"}}, ["query"]),
}


def tool_list():
    return [{"name": n, "description": d,
             "inputSchema": {"type": "object", "properties": p, "required": req},
             "annotations": {"readOnlyHint": True, "openWorldHint": False}}
            for n, (_, d, p, req) in TOOLS.items()]


def handle(msg):
    """One JSON-RPC message in; the reply dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "openvitals", "version": "1"},
            "instructions": "Your own strap data from this Mac. Start with `today` or `readiness`; use "
                            "`schema` then `sql` for anything else. " + NOTE}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            out = TOOLS[name][0](**(params.get("arguments") or {}))
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, default=str)}], "isError": False}}
        except Exception as ex:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"{type(ex).__name__}: {ex}"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(msg)
            except Exception:
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": msg.get("id"), "error": {"code": -32603, "message": "internal error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
