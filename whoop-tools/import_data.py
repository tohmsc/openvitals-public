#!/usr/bin/env python3
"""Device adapter: load YOUR wearable's data into the dashboard from simple CSVs.

The dashboard reads a SQLite DB (schema in db.py). The WHOOP pipeline fills it from the
strap + API, but any device works if you put its export into these templates:

  templates/daily.csv       one row per day: recovery, HRV, RHR, SpO2, skin temp, sleep, strain
  templates/workouts.csv    one row per workout
  templates/heart_rate.csv  optional HR samples (timestamp, bpm)

Every column except `date` / `start` / `timestamp` is optional; leave unknown cells blank.
See templates/README.md for the column meanings and how to map Oura/Garmin/Apple Health/etc.

Usage:
  python import_data.py --dir my_data/                 # reads daily.csv, workouts.csv, heart_rate.csv
  python import_data.py --daily d.csv --workouts w.csv --db whoop.db
  python import_data.py --dir templates/ --dry-run     # validate without writing
Re-running is safe: rows are upserted by date / start time.
"""
import argparse, csv, datetime as dt, os, sys
import db

DAILY_REQ, WORKOUT_REQ, HR_REQ = "date", "start", "timestamp"


def num(v, cast=float):
    v = (v or "").strip()
    if not v:
        return None
    try:
        return cast(float(v))
    except ValueError:
        raise ValueError(f"not a number: {v!r}")


def iso(v):
    """Accept 2025-01-01, 2025-01-01T07:05:00, '2025-01-01 07:05', trailing Z/offset."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"not an ISO date/time: {v!r}")
    if d.tzinfo:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d.isoformat() + "Z"


def read(path, required):
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        if required not in (r.fieldnames or []):
            sys.exit(f"{path}: missing required column '{required}' (have: {r.fieldnames})")
        for n, row in enumerate(r, start=2):
            if (row.get(required) or "").strip():
                yield n, {k.strip(): (v or "") for k, v in row.items() if k}


def daily_rows(row):
    day = iso(row["date"])
    key = day[:10]
    cid = int(key.replace("-", ""))          # stable id per day, e.g. 20250101
    sid = f"import-{key}"
    out = []
    ms = lambda m: None if m is None else int(m * 60000)
    light, deep, rem, awake = (num(row.get(k)) for k in ("light_min", "deep_min", "rem_min", "awake_min"))
    stages = [x for x in (light, deep, rem, awake) if x is not None]
    has_sleep = stages or row.get("sleep_start") or row.get("sleep_perf_pct")
    if has_sleep:
        out.append(("api_sleep", {
            "id": sid, "start": iso(row.get("sleep_start")) or day, "end": iso(row.get("sleep_end")),
            "nap": 0, "resp_rate": num(row.get("resp_rate")), "sleep_perf": num(row.get("sleep_perf_pct")),
            "total_in_bed_ms": ms(sum(stages)) if stages else None, "total_awake_ms": ms(awake),
            "total_light_ms": ms(light), "total_sws_ms": ms(deep), "total_rem_ms": ms(rem),
            "disturbances": num(row.get("disturbances"), int)}))
    rec = {k: num(row.get(c)) for k, c in (("recovery_score", "recovery_pct"), ("hrv_rmssd_ms", "hrv_ms"),
           ("resting_hr", "resting_hr"), ("spo2", "spo2_pct"), ("skin_temp_c", "skin_temp_c"))}
    if any(v is not None for v in rec.values()):
        out.append(("api_recovery", {"cycle_id": cid, "sleep_id": sid if has_sleep else None,
                                     "created_at": day, "score_state": "SCORED", **rec}))
    cyc = {k: num(row.get(c)) for k, c in (("strain", "strain"), ("avg_hr", "avg_hr"),
           ("max_hr", "max_hr"), ("kilojoule", "kilojoules"))}
    if any(v is not None for v in cyc.values()):
        end = (dt.datetime.fromisoformat(day[:-1]) + dt.timedelta(days=1)).isoformat() + "Z"
        out.append(("api_cycle", {"id": cid, "start": day, "end": end, "score_state": "SCORED", **cyc}))
    return out


def workout_rows(row):
    start = iso(row["start"])
    return [("api_workout", {
        "id": f"import-{start}", "start": start, "end": iso(row.get("end")),
        "sport_name": (row.get("sport") or "activity").strip().lower(), "score_state": "SCORED",
        "strain": num(row.get("strain")), "avg_hr": num(row.get("avg_hr")), "max_hr": num(row.get("max_hr")),
        "kilojoule": num(row.get("kilojoules")), "distance_m": num(row.get("distance_m"))})]


def hr_rows(row):
    ts = dt.datetime.fromisoformat(iso(row["timestamp"])[:-1]).replace(tzinfo=dt.timezone.utc).timestamp()
    return [("ble_hr", {"ts": ts, "hr": num(row.get("hr"), int), "rr_ms": ""})]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", help="folder containing daily.csv / workouts.csv / heart_rate.csv")
    ap.add_argument("--daily"); ap.add_argument("--workouts"); ap.add_argument("--heart-rate")
    ap.add_argument("--db", help=f"target SQLite file (default: {db.DB_PATH})")
    ap.add_argument("--dry-run", action="store_true", help="validate and count, write nothing")
    a = ap.parse_args()

    files = {"daily": a.daily, "workouts": a.workouts, "heart_rate": a.heart_rate}
    if a.dir:
        for k in files:
            p = os.path.join(a.dir, f"{k}.csv")
            if not files[k] and os.path.exists(p):
                files[k] = p
    plan = [(files["daily"], DAILY_REQ, daily_rows), (files["workouts"], WORKOUT_REQ, workout_rows),
            (files["heart_rate"], HR_REQ, hr_rows)]
    if not any(p for p, _, _ in plan):
        ap.error("give --dir or at least one of --daily / --workouts / --heart-rate")

    if a.db:
        db.DB_PATH = a.db
    c = None if a.dry_run else db.connect()
    counts, errors = {}, []
    for path, req, fn in plan:
        if not path:
            continue
        for line, row in read(path, req):
            try:
                for table, rec in fn(row):
                    counts[table] = counts.get(table, 0) + 1
                    if c:
                        db.upsert(c, table, rec)
            except ValueError as e:
                errors.append(f"{os.path.basename(path)}:{line}: {e}")
    if c:
        c.commit(); c.close()
    for e in errors[:20]:
        print("skip", e, file=sys.stderr)
    verb = "would import" if a.dry_run else f"imported into {db.DB_PATH}"
    print(f"{verb}: " + (", ".join(f"{t}={n}" for t, n in sorted(counts.items())) or "nothing")
          + (f"  ({len(errors)} bad rows skipped)" if errors else ""))
    return 1 if errors and not counts else 0


if __name__ == "__main__":
    sys.exit(main())
