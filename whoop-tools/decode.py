#!/usr/bin/env python3
"""Decode captured raw strap frames (ble_raw) into structured tables:
  - realtime_hr : verified HR + RR from REALTIME_DATA (0x28)
  - device_soc  : battery state-of-charge over time (from firmware SOC reports)
  - device_logs : reassembled firmware console text (0x32), one row per log line
The event-framed historical sensor records (0x30 #3/#63) are kept raw in
ble_raw / history_*.ndjson; their per-field layout is not yet fully verified,
so we don't assert labels we aren't sure of.
"""
import db, struct, re

EXTRA = """
CREATE TABLE IF NOT EXISTS realtime_hr (rtc INTEGER PRIMARY KEY, hr INTEGER, rr_1024 TEXT);
CREATE TABLE IF NOT EXISTS device_soc (rtc INTEGER PRIMARY KEY, soc_pct REAL, subsystem TEXT);
CREATE TABLE IF NOT EXISTS device_logs (rtc INTEGER, subsystem TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_logs_rtc ON device_logs(rtc);
-- raw per-sample sensor records from the historical event stream.
-- event_cmd 63 = R10/R11 raw optical (PPG); 3 = paired sensor record.
-- i16_json is the payload parsed as little-endian int16s (field semantics are
-- candidate/unverified for this firmware's event format); hex is the lossless payload.
CREATE TABLE IF NOT EXISTS strap_samples (
  ts INTEGER, sub INTEGER, event_cmd INTEGER, n_i16 INTEGER, i16_json TEXT, hex TEXT);
CREATE INDEX IF NOT EXISTS idx_samples ON strap_samples(ts, event_cmd);
"""

LINE_RE = re.compile(r"(\d+):\s*([A-Za-z0-9_]+):\s*(.*)")
SOC_RE  = re.compile(r"SOC report:\s*([0-9.]+)")

def run():
    c = db.connect()
    c.executescript(EXTRA)
    c.execute("DELETE FROM realtime_hr"); c.execute("DELETE FROM device_soc"); c.execute("DELETE FROM device_logs")
    rows = [bytes.fromhex(r["hex"]) for r in c.execute("SELECT hex FROM ble_raw ORDER BY ts").fetchall()]

    # REALTIME_DATA
    rt = 0
    for inner in rows:
        if inner and inner[0] == 0x28 and len(inner) >= 18:
            rtc = struct.unpack_from("<I", inner, 2)[0]
            hr = inner[8]; cnt = inner[9]
            rr = [struct.unpack_from("<H", inner, 10 + 2*i)[0] for i in range(min(cnt, 4))]
            c.execute("INSERT OR REPLACE INTO realtime_hr(rtc,hr,rr_1024) VALUES(?,?,?)",
                      (rtc, hr, " ".join(map(str, rr)))); rt += 1

    # CONSOLE_LOGS: reassemble the fragmented ASCII stream, strip 34 00 01 markers
    blob = bytearray()
    for inner in rows:
        if inner and inner[0] == 0x32:
            blob += inner[3:][7:]
    text = bytes(blob).replace(b"\x34\x00\x01", b"").decode("ascii", "ignore")
    # the stream is "<lvl>, <rtc>: <SUBSYS>: <msg>. " — split on the ", <digits>:" anchor
    parts = re.split(r"\s*\d+,\s*", text)
    logs = soc = 0
    for p in parts:
        m = LINE_RE.match(p.strip())
        if not m:
            continue
        rtc, subsys, msg = int(m.group(1)), m.group(2), m.group(3).strip(" .")
        c.execute("INSERT INTO device_logs(rtc,subsystem,message) VALUES(?,?,?)", (rtc, subsys, msg))
        logs += 1
        sm = SOC_RE.search(msg)
        if sm:
            c.execute("INSERT OR REPLACE INTO device_soc(rtc,soc_pct,subsystem) VALUES(?,?,?)",
                      (rtc, float(sm.group(1)), subsys)); soc += 1
    # strap sensor samples from event stream (type 0x30, cmd 3 and 63)
    import json as _json
    c.execute("DELETE FROM strap_samples")
    samp = 0
    for inner in rows:
        if inner and inner[0] == 0x30 and len(inner) >= 3 and inner[2] in (3, 63):
            d = inner[3:]
            if len(d) < 9:
                continue
            ts = struct.unpack_from("<I", d, 1)[0]
            sub = struct.unpack_from("<H", d, 5)[0]
            plen = struct.unpack_from("<H", d, 7)[0]
            pay = d[9:9 + plen]
            i16 = [struct.unpack_from("<h", pay, i)[0] for i in range(0, len(pay) - 1, 2)]
            c.execute("INSERT INTO strap_samples(ts,sub,event_cmd,n_i16,i16_json,hex) VALUES(?,?,?,?,?,?)",
                      (ts, sub, inner[2], len(i16), _json.dumps(i16), pay.hex()))
            samp += 1
    c.commit()
    print(f"decoded: realtime_hr={rt}  device_logs={logs}  device_soc={soc}  strap_samples={samp}")
    # quick peek
    for r in c.execute("SELECT subsystem, COUNT(*) n FROM device_logs GROUP BY subsystem ORDER BY n DESC LIMIT 12"):
        print(f"  {r['subsystem']:20s} {r['n']}")

if __name__ == "__main__":
    run()
