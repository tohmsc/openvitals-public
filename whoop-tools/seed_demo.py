#!/usr/bin/env python3
"""Generate a SYNTHETIC demo database: a fill-in template, no real person's data.

Produces a populated whoop.db-shaped database so a new user (or their agent) can open
the dashboard and see every section working before connecting their own strap/API.
All values are randomly generated for a fictional "Demo User". Dates are relative to
today, so the demo always looks current. Replace it by syncing your own device.

Usage:
  python seed_demo.py [OUTPUT_DB]        # default: data/whoop-demo.db
  WHOOP_DB=data/whoop-demo.db python dashboard.py   # preview it
  python seed_demo.py --stream [DB]      # fake a live strap: synthetic HR/RR every second
"""
import os, sys, sqlite3, random, math, json, time, datetime as dt
import db     # reuse the real schema
import decode  # device_soc / device_logs schema

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT = ARGS[0] if ARGS else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "whoop-demo.db")
DAYS = 120
random.seed(42)  # reproducible values (dates still move with "today")

H = 3.6e6  # ms per hour
ZONE_KEYS = ["zone_zero_milli", "zone_one_milli", "zone_two_milli",
             "zone_three_milli", "zone_four_milli", "zone_five_milli"]
# sport -> (minutes range, relative time-in-zone weights Z0..Z5, distance m/min or 0)
SPORTS = {
    "running":            ((30, 70), [1, 6, 22, 38, 25, 8], 170),
    "cycling":            ((45, 110), [2, 10, 35, 33, 16, 4], 450),
    "functional-fitness": ((35, 60), [6, 18, 30, 26, 15, 5], 0),
    "weightlifting":      ((40, 75), [20, 40, 28, 10, 2, 0], 0),
    "yoga":               ((30, 60), [45, 45, 10, 0, 0, 0], 0),
}


def utc(local_naive):
    """Local wall-clock time -> ISO UTC string like the WHOOP API returns."""
    return local_naive.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jitter_minutes(sd):
    return dt.timedelta(minutes=random.gauss(0, sd))


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        os.remove(OUT)
    c = sqlite3.connect(OUT)
    c.executescript(db.SCHEMA)
    c.executescript(decode.EXTRA)

    today = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    first = today - dt.timedelta(days=DAYS - 1)
    fitness = 0.0          # slow drift so the trends have shape
    prev_strain = 10.0
    prev_sleep_start = None
    cycles = []

    for i in range(DAYS):
        d = first + dt.timedelta(days=i)               # the morning we wake up
        fitness = 0.97 * fitness + random.gauss(0, 1.2)
        weekend = d.weekday() >= 5

        # ---- sleep: evening of d-1 -> morning of d -------------------------
        bed = d - dt.timedelta(days=1) + dt.timedelta(hours=22, minutes=50 if not weekend else 70) + jitter_minutes(35)
        in_bed_h = min(9.6, max(5.4, random.gauss(8.0 if weekend else 7.5, 0.6) - 0.04 * (prev_strain - 10)))
        wake = bed + dt.timedelta(hours=in_bed_h)
        in_bed = in_bed_h * H
        awake = in_bed * random.uniform(0.05, 0.12)
        asleep = in_bed - awake
        sws = asleep * random.uniform(0.17, 0.25)
        rem = asleep * random.uniform(0.20, 0.27)
        light = asleep - sws - rem
        perf = round(min(100, max(48, asleep / (7.6 * H) * 100 + random.gauss(0, 4))))
        consistency = None
        if prev_sleep_start is not None:
            drift = abs((bed - prev_sleep_start - dt.timedelta(days=1)).total_seconds()) / 60
            consistency = round(max(40, min(98, 95 - drift * 0.6 + random.gauss(0, 3))))
        prev_sleep_start = bed
        sid = f"demo-sleep-{i:03d}"
        c.execute("INSERT INTO api_sleep(id,start,end,nap,resp_rate,sleep_perf,sleep_consistency,"
                  "sleep_efficiency,total_in_bed_ms,total_awake_ms,total_light_ms,total_sws_ms,"
                  "total_rem_ms,disturbances) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sid, utc(bed), utc(wake), 0, round(random.gauss(14.8, 0.4), 1), perf, consistency,
                   round(asleep / in_bed * 100, 1), int(in_bed), int(awake), int(light), int(sws),
                   int(rem), random.randint(3, 14)))
        if random.random() < 0.06:  # occasional nap (hidden from the nightly charts)
            ns = d + dt.timedelta(hours=14, minutes=random.randint(0, 90))
            nl = random.uniform(0.3, 0.7) * H
            c.execute("INSERT INTO api_sleep(id,start,end,nap,total_in_bed_ms,total_awake_ms,"
                      "total_light_ms,total_sws_ms,total_rem_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                      (f"demo-nap-{i:03d}", utc(ns), utc(ns + dt.timedelta(milliseconds=nl)), 1,
                       int(nl), int(nl * 0.1), int(nl * 0.6), int(nl * 0.2), int(nl * 0.1)))

        # ---- recovery: scored on waking --------------------------------------
        hrv = max(28, random.gauss(68 + 2.2 * fitness + 0.25 * (perf - 80) - 0.8 * (prev_strain - 10), 9))
        rhr = round(max(42, random.gauss(52 - 0.5 * fitness + 0.25 * (prev_strain - 10), 1.8)))
        recovery = round(min(99, max(4, (hrv - 35) * 1.25 + (perf - 70) * 0.35 + random.gauss(14, 9))))
        cid = 900000 + i
        c.execute("INSERT INTO api_recovery(cycle_id,sleep_id,created_at,score_state,recovery_score,"
                  "resting_hr,hrv_rmssd_ms,spo2,skin_temp_c) VALUES(?,?,?,?,?,?,?,?,?)",
                  (cid, sid, utc(wake + dt.timedelta(minutes=random.randint(4, 25))), "SCORED",
                   recovery, rhr, round(hrv, 2), round(random.uniform(94.5, 98.5), 1),
                   round(random.gauss(33.9, 0.3), 2)))

        # ---- workouts: ~4-5 a week, mornings or evenings ---------------------
        day_strain_extra = 0.0
        workout_today = random.random() < (0.45 if weekend else 0.62)
        if workout_today:
            sport = random.choices(list(SPORTS), weights=[4, 2, 2, 3, 1])[0]
            (lo, hi), weights, mpm = SPORTS[sport]
            minutes = random.randint(lo, hi)
            hour = random.choice([6, 7, 7, 12, 17, 18, 18, 19]) if not weekend else random.choice([8, 9, 10, 16])
            ws = d + dt.timedelta(hours=hour, minutes=random.choice([0, 10, 15, 30, 40, 45]))
            if ws < wake:
                ws = wake + dt.timedelta(minutes=45)
            we = ws + dt.timedelta(minutes=minutes)
            w = [max(0.0, x * random.uniform(0.75, 1.25)) for x in weights]
            dur_ms = minutes * 6e4
            zones = {k: int(dur_ms * x / sum(w)) for k, x in zip(ZONE_KEYS, w)}
            intensity = sum(i * x for i, x in enumerate(w)) / sum(w)       # ~0.6 .. 3.2
            wstrain = round(min(19.5, 3 + intensity * 2.6 + minutes / 30 + random.gauss(0, 0.8)), 1)
            avg = round(95 + intensity * 20 + random.gauss(0, 4))
            mx = round(min(194, avg + random.uniform(18, 35)))
            kj = round(minutes * (18 + intensity * 14) * random.uniform(0.9, 1.1))
            dist = round(minutes * mpm * random.uniform(0.85, 1.15)) if mpm else None
            c.execute("INSERT INTO api_workout(id,start,end,sport_name,score_state,strain,avg_hr,"
                      "max_hr,kilojoule,distance_m,zone_raw) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (f"demo-wk-{i:03d}", utc(ws), utc(we), sport, "SCORED", wstrain, avg, mx, kj,
                       dist, json.dumps(zones)))
            day_strain_extra = wstrain * 0.55

        # ---- cycle (day strain): starts at sleep onset ----------------------
        strain = round(min(20.5, max(3.5, random.gauss(7.5, 1.6) + day_strain_extra)), 1)
        cycles.append([cid, bed, strain])
        prev_strain = strain

    for k, (cid, start, strain) in enumerate(cycles):
        end = cycles[k + 1][1] if k + 1 < len(cycles) else None   # today's cycle is still open
        c.execute("INSERT INTO api_cycle(id,start,end,score_state,strain,avg_hr,max_hr,kilojoule) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (cid, utc(start), utc(end) if end else None, "SCORED", strain,
                   round(58 + strain * 1.4 + random.gauss(0, 2)), round(120 + strain * 3 + random.gauss(0, 5)),
                   round(7200 + strain * 420 + random.gauss(0, 300))))

    # ---- strap side: ~15 min of 1 Hz heart rate ending a few minutes ago ----------
    now = time.time()
    t0 = now - 4 * 60 - 900
    hr = 64.0
    for s in range(900):
        hr += random.gauss(0, 0.6) + (64 + 6 * math.sin(s / 140) - hr) * 0.05
        bpm = int(round(hr))
        rr = round(60000 / bpm)
        c.execute("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", (t0 + s, bpm, str(rr)))

    # ---- firmware telemetry: battery SOC reports + console log lines ------------
    rtc0 = int(now) - 36 * 3600
    soc = 96.0
    logs = []
    for k in range(72):                       # one SOC report every 30 min for 36 h
        rtc = rtc0 + k * 1800
        soc = max(8.0, soc - random.uniform(0.35, 0.6))
        logs.append((rtc, "SUPERVISOR", f"SOC report: {soc:.2f}"))
        c.execute("INSERT INTO device_soc(rtc,soc_pct,subsystem) VALUES(?,?,?)", (rtc, round(soc, 2), "SUPERVISOR"))
        if k % 4 == 1:
            logs.append((rtc + 7, "MAX77818_MANAGER", f"VCELL {3.55 + soc / 400:.3f} V, ICHG 0 mA, temp {random.uniform(29, 33):.1f} C"))
        if k % 6 == 2:
            logs.append((rtc + 31, "BLE_MANAGER", "Connection parameters updated: interval 30 ms, latency 0"))
        if k % 6 == 5:
            logs.append((rtc + 52, "PPG_MANAGER", f"LED drive adjusted, ambient {random.randint(3, 40)}, SNR ok"))
        if k % 12 == 9:
            logs.append((rtc + 90, "HISTORY", f"Flash pages committed: {random.randint(3, 9)}, ring buffer {random.randint(4, 30)}% full"))
        if k % 18 == 3:
            logs.append((rtc + 140, "IMU_MANAGER", "Wrist-on detected, motion level low"))
    c.executemany("INSERT INTO device_logs(rtc,subsystem,message) VALUES(?,?,?)", logs)

    c.executemany("INSERT OR REPLACE INTO api_profile(key,value) VALUES(?,?)",
                  [("first_name", "Demo"), ("last_name", "User")])
    c.executemany("INSERT OR REPLACE INTO sync_state(key,value) VALUES(?,?)",
                  [("last_ble_sync", str(now - 4 * 60)), ("api_since", utc(today))])
    c.commit()
    n = sum(c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("api_recovery", "api_sleep", "api_cycle", "api_workout", "ble_hr", "device_logs"))
    c.close()
    print(f"synthetic demo DB -> {OUT}  ({n} rows, fictional data)")


def stream():
    """Write a synthetic live heartbeat (same shape live.py writes) until Ctrl-C."""
    c = sqlite3.connect(OUT)
    t, hr = 0, 62.0
    print(f"streaming fake live HR into {OUT} (Ctrl-C to stop)")
    try:
        while True:
            hr = max(48, min(110, hr + random.gauss(0, 0.6) + 0.15 * (62 - hr)))
            cur = round(hr + 3 * math.sin(t / 4.5))   # breathing-driven variation
            rr = [round(60000 / cur + random.gauss(0, 18))]
            now = time.time()
            c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", (now, cur, " ".join(map(str, rr))))
            c.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES('live_hr_now',?)",
                      (json.dumps({"hr": cur, "rr_ms": rr, "ts": now}),))
            c.commit()
            t += 1
            time.sleep(1)
    except KeyboardInterrupt:
        c.execute("INSERT OR REPLACE INTO sync_state(key,value) VALUES('live_hr_now',?)",
                  (json.dumps({"hr": None, "ts": time.time()}),))
        c.commit()


if __name__ == "__main__":
    stream() if "--stream" in sys.argv else main()
