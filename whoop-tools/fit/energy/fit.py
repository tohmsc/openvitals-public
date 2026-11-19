# How scores.ENERGY_V1 was fitted (energy per day, kcal). Needs numpy + scipy:
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   sqlite3 -readonly whoop.db ".backup /tmp/energy.db"
#   WHOOP_DB=/tmp/energy.db python energy.py set sex male      # and weight_kg, height_cm, age
#   WHOOP_DB=/tmp/energy.db /tmp/fitenv/bin/python fit/energy/fit.py
#
# Ground truth: WHOOP's energy per cycle (api_cycle.kilojoule) and per workout
# (api_workout.kilojoule). The archive has no per-second heart rate, so each item's heart rate
# is rebuilt from what WHOOP stored:
#   workout  minutes in WHOOP's zones (zone k = k*10+40 .. k*10+50 % of HR reserve; % of max HR
#            before Dec 2024), spread evenly inside each zone; zone 0 spread so the workout's
#            average heart rate matches WHOOP's.
#   day      sleeps from api_sleep at resting HR + 5; the day's workouts as above; the rest of
#            the time at the mean that makes the day's average match WHOOP's, with the spread
#            of awake heart rate the strap recorded (ble_hr outside sleep and workouts).
# The same energy.py formulas are then applied minute-by-minute. Free parameters (threshold on
# HR reserve, active scale, resting factors) are fitted on the first 70% of days by date and
# reported on the last 30% (workouts split at the same date).
#
# Result (2026-11-19, the owner's archive; numbers in scores.ENERGY_V1):
# - Plain formulas overshoot: Keytel above 30% of reserve, 29.9% error on days, 67.9% on workouts.
# - Fitted threshold 0.35 and scale ~0.6 (resting = Mifflin as is): 16.5% on held-out days,
#   18.5% on workouts. A typical-day guess with no heart rate gets 16.1% on these days (58.7% on
#   workouts): WHOOP's day energy fell ~20% from 2024 to 2025 at the same average HR (weight or
#   method change at WHOOP?), and rebuilt days are only approximate.
# - Lower thresholds fit rebuilt days slightly better but already exceed WHOOP's number on the
#   one archive day the strap also recorded second by second, so 0.35 was chosen (see sweep).
# - The "zones" rebuild (day strain inverted through MODELS["v1"]) gave inconsistent fits and
#   is kept only as a sensitivity check.
import os, sys, json, math, calendar, time, sqlite3
import numpy as np
from scipy.optimize import least_squares
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import energy

DB = os.environ.get("WHOOP_DB", "whoop.db")
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
body, missing = energy.profile(c)
if not body:
    sys.exit(f"set your body data first (WHOOP_DB={DB} python energy.py set ...): missing {missing}")
MAX_HR = 197.0                       # scores.MODELS["v1"]["strain"]["max_hr"]
ZN = ["zone_zero_milli", "zone_one_milli", "zone_two_milli", "zone_three_milli", "zone_four_milli", "zone_five_milli"]
U = lambda s: calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + float("0." + (s[20:23] or "0"))

# ---------- resting HR per cycle ----------
cyc = [dict(zip(("id", "start", "end", "avg", "kj"), r)) for r in c.execute(
    "SELECT id, start, end, avg_hr, kilojoule FROM api_cycle WHERE end IS NOT NULL AND kilojoule > 0 AND avg_hr > 0 ORDER BY start")]
rhr = dict(c.execute("SELECT cycle_id, resting_hr FROM api_recovery WHERE resting_hr > 0"))
med_rest = float(np.median(list(rhr.values())))
for x in cyc:
    x["a"], x["b"], x["rest"] = U(x["start"]), U(x["end"]), rhr.get(x["id"], med_rest)
def rest_at(t):
    for x in cyc:
        if x["a"] <= t < x["b"]:
            return x["rest"]
    return med_rest

# ---------- awake heart-rate spread from the strap ----------
sleeps = [(U(s), U(e)) for s, e in c.execute("SELECT start, end FROM api_sleep WHERE start IS NOT NULL AND end IS NOT NULL")]
sleeps += [(a, b) for a, b in c.execute("SELECT onset, wake FROM sleep_night WHERE onset AND wake")] if c.execute(
    "SELECT 1 FROM sqlite_master WHERE name='sleep_night'").fetchone() else []
works = [(U(s), U(e)) for s, e in c.execute("SELECT start, end FROM api_workout WHERE start IS NOT NULL AND end IS NOT NULL")]
hr = np.array(c.execute("SELECT ts, hr FROM ble_hr WHERE hr > 0 ORDER BY ts").fetchall(), dtype=float)
keep = np.ones(len(hr), bool)
for a, b in sleeps + works:
    keep &= ~((hr[:, 0] >= a) & (hr[:, 0] < b))
aw = hr[keep]
days = np.floor((aw[:, 0] + 3600) / 86400)
dev = np.concatenate([aw[days == d, 1] - aw[days == d, 1].mean() for d in np.unique(days) if (days == d).sum() > 3600])
QS = np.quantile(dev, (np.arange(40) + 0.5) / 40)          # 40 equal-weight points
print(f"awake spread from {len(dev)} strap seconds: sd {dev.std():.1f} bpm, p10 {QS[4]:.0f} p90 {QS[35]:.0f}")
SLEEP_DEV = np.array([-3.0, 0.0, 3.0, 6.0, 9.0])          # sleeping HR = rest + 5 +- a little

# ---------- rebuilt heart-rate histograms: list of (hr array, minutes array) ----------
def workout_hist(w, rest):
    z = json.loads(w["zone_raw"])
    mins = np.array([z[k] / 6e4 for k in ZN])
    hrr = w["start"] >= "2024-12"
    lo_ref, span = (rest, MAX_HR - rest) if hrr else (0.0, MAX_HR)
    hrs, ms = [], []
    for k in range(1, 6):
        if mins[k] > 0:
            f = 0.5 + 0.1 * (k - 1) + 0.1 * (np.arange(5) + 0.5) / 5
            hrs.append(lo_ref + f * span); ms.append(np.full(5, mins[k] / 5))
    top0 = lo_ref + 0.5 * span
    if mins[0] > 0:
        rest_sum = w["avg"] * mins.sum() - sum((h * m).sum() for h, m in zip(hrs, ms))
        m0 = np.clip(rest_sum / mins[0], rest + 2, top0 - 1)
        lo0 = max(rest, 2 * m0 - top0)
        h0 = lo0 + (2 * (m0 - lo0)) * (np.arange(5) + 0.5) / 5
        hrs.append(h0); ms.append(np.full(5, mins[0] / 5))
    return np.concatenate(hrs) if hrs else np.zeros(0), np.concatenate(ms) if ms else np.zeros(0)

W = []
for r in c.execute("SELECT start, end, avg_hr, kilojoule, zone_raw, sport_name FROM api_workout WHERE zone_raw LIKE '%zone_zero%' "
                   "AND kilojoule > 0 AND avg_hr > 0 ORDER BY start"):
    w = dict(zip(("start", "end", "avg", "kj", "zone_raw", "sport"), r))
    w["a"], w["b"] = U(w["start"]), U(w["end"])
    dur = (w["b"] - w["a"]) / 60
    zsum = sum(json.loads(w["zone_raw"]).values()) / 6e4
    if dur < 5 or zsum < 0.8 * dur:                  # logged by hand / heart rate not recorded throughout
        continue
    w["rest"] = rest_at(w["a"])
    w["h"], w["m"] = workout_hist(w, w["rest"])
    w["min"] = dur
    W.append(w)


# ---------- days ----------
# Two ways to rebuild the time outside sleep and workouts:
#   "avg"    its mean from the day's average HR, the strap's awake spread around it (all days).
#   "zones"  also WHOOP's day strain: inverting scores.MODELS["v1"] strain gives the day's
#            weighted zone minutes; what the workouts don't explain is put in zone 1 (50-60% of
#            HR reserve), the rest below 50%. Dec 2024 on only (HR-reserve zones, as v1 strain).
S1 = {"max_hr": 197.0, "weights": [0.27996, 1.0, 3.82264, 5.87113, 10.08847, 15.32562],
      "slope": 0.61956, "offset": 4.89692, "zone0_share": [0.6074, 0.0327]}     # scores.MODELS["v1"]["strain"]

def day_hist(x, mode):
    span = (x["b"] - x["a"]) / 60
    hs, ms, used = [], [], 0.0
    sl = min(sum(max(0.0, min(b, x["b"]) - max(a, x["a"])) for a, b in sleeps) / 60, span * 0.8)
    if sl:
        hs.append(x["rest"] + 5 + SLEEP_DEV); ms.append(np.full(len(SLEEP_DEV), sl / len(SLEEP_DEV)))
        used += (x["rest"] + 5) * sl
    wmin, wz = 0.0, np.zeros(6)
    for w in W:
        if x["a"] <= w["a"] < x["b"]:
            hs.append(w["h"]); ms.append(w["m"]); wmin += w["m"].sum(); used += (w["h"] * w["m"]).sum()
            z = json.loads(w["zone_raw"]); wz += np.array([z[k] / 6e4 for k in ZN])
    other = span - sl - wmin
    if other < 60:
        return None
    top50 = x["rest"] + 0.5 * (MAX_HR - x["rest"])
    if mode == "zones":
        load = math.exp(S1["offset"] + math.tan(math.pi * (min(x["strain"], 20.9) / 21 - 0.5)) / S1["slope"])
        w = S1["weights"]; p0, p1 = S1["zone0_share"]
        share = min(1.0, max(0.0, p0 + p1 * (x["avg"] - 70)))
        lw = float(np.dot(w[2:], wz[2:])) + wz[1]
        z1 = (load - lw - w[0] * share * (span - wz[1:].sum())) / (1 - w[0] * share)
        z1 = float(np.clip(z1, 0.0, other * 0.6))
        if z1 > 0:
            h1 = x["rest"] + (0.5 + 0.1 * (np.arange(5) + 0.5) / 5) * (MAX_HR - x["rest"])
            hs.append(h1); ms.append(np.full(5, z1 / 5)); used += h1.mean() * z1
        other -= z1
    mean = np.clip((x["avg"] * span - used) / other, x["rest"] + 5, 140)
    h = np.maximum(x["rest"], mean + QS)
    if mode == "zones":
        h = np.minimum(h, top50 - 0.5)
    hs.append(h); ms.append(np.full(len(QS), other / len(QS)))
    return np.concatenate(hs), np.concatenate(ms), span, sl

cyc_s = dict(c.execute("SELECT id, strain FROM api_cycle"))
DAYS = {}
for mode in ("avg", "zones"):
    D = []
    for x in cyc:
        span = (x["b"] - x["a"]) / 60
        if not 12 * 60 <= span <= 40 * 60:
            continue
        x = dict(x, strain=cyc_s.get(x["id"]) or 0.0)
        if mode == "zones" and (x["start"] < "2024-12" or not x["strain"]):
            continue
        r = day_hist(x, mode)
        if r:
            x["h"], x["m"], x["min"], x["sleep_min"] = r
            D.append(x)
    DAYS[mode] = D
for w in W:
    w["sleep_min"] = 0.0

# ---------- features: resting kcal asleep / awake, active kcal A(t) ----------
def base_rate(date):
    return energy.bmr_kcal_day(body["sex"], body["weight_kg"], body["height_cm"], energy.age_on(body, date)) / 1440

def feats(item, t):
    date = item["start"][:10]
    age = energy.age_on(body, date)
    base = base_rate(date)
    kt = energy.keytel_kcal_min(body["sex"], item["h"], body["weight_kg"], age)
    f = (item["h"] - item["rest"]) / (MAX_HR - item["rest"])
    act = np.where(f >= t, np.maximum(0.0, kt - base), 0.0)
    return base * item["sleep_min"], base * (item["min"] - item["sleep_min"]), float((act * item["m"]).sum())

for x in W + DAYS["avg"] + DAYS["zones"]:
    x["kcal"] = x["kj"] / energy.KJ_PER_KCAL
    x["bmr_ok"] = x["kcal"] >= base_rate(x["start"][:10]) * x["min"]
cut = DAYS["avg"][int(len(DAYS["avg"]) * 0.7)]["start"]
split = lambda L: ([x for x in L if x["start"] < cut], [x for x in L if x["start"] >= cut])
Wtr, Wte = split(W)
for mode, D in DAYS.items():
    tr, te = split(D)
    print(f"days ({mode}): {len(D)} (train {len(tr)}, test {len(te)}); "
          f"{sum(not x['bmr_ok'] for x in D)} with WHOOP under the resting rate (strap off part of the day?)")
print(f"workouts {len(W)} (train {len(Wtr)}, test {len(Wte)}); split at {cut[:10]}")

def pred(items, t, ks, ka, s):
    return np.array([ks * a + ka * b + s * d for a, b, d in (feats(x, t) for x in items)])

def errs(items, p):
    y = np.array([x["kcal"] for x in items]); e = pred(items, *p) - y
    return e, y

def report(items, p):
    if not items:
        return "n=0"
    e, y = errs(items, p)
    return f"n={len(y):3d} MAE {np.abs(e).mean():4.0f} kcal ({100 * np.abs(e / y).mean():4.1f}%)  bias {e.mean():+5.0f}"

def fit(items, t, shape):
    """shape: 'scale' (k=1, fit s), 'k' (one resting factor), 'k2' (asleep and awake factors)."""
    F = np.array([feats(x, t) for x in items]); y = np.array([x["kcal"] for x in items])
    unpack = {"scale": lambda q: (1.0, 1.0, q[0]), "k": lambda q: (q[0], q[0], q[1]), "k2": lambda q: tuple(q)}[shape]
    def res(q):
        ks, ka, s = unpack(q)
        return (F @ [ks, ka, s] - y) / y
    q0 = {"scale": [1.0], "k": [1.0, 1.0], "k2": [1.0, 1.0, 1.0]}[shape]
    q = least_squares(res, q0, loss="soft_l1", f_scale=0.05, bounds=(0, 5)).x
    ks, ka, s = unpack(q)
    return float(np.abs(res(q)).mean()), (t, ks, ka, s)

def best(items, shape):
    return min((fit(items, t, shape) for t in GRID), key=lambda r: r[0])[1]

GRID = np.round(np.arange(0.0, 0.61, 0.05), 2)
models = [("plain: BMR + Keytel above resting rate, every second", (0.0, 1, 1, 1)),
          ("plain: BMR + Keytel above 30% of HR reserve", (0.3, 1, 1, 1))]
for mode in ("avg", "zones"):
    tr = split(DAYS[mode])[0]
    for shape in ("scale", "k", "k2"):
        models.append((f"fit on days ({mode}), {shape}", best(tr, shape)))
    models.append((f"fit on days ({mode}) + workouts, k2", best(tr + Wtr, "k2")))
    models.append((f"fit on days ({mode}) + workouts, scale", best(tr + Wtr, "scale")))
models.append(("fit on workouts, k", best(Wtr, "k")))
models.append(("fit on workouts, scale", best(Wtr, "scale")))

# ---------- the real-heart-rate check: WHOOP cycles the strap also recorded second by second ----------
# Only a part of such a day is covered; counting the rest at the resting rate gives the least
# the model would say for the whole day. If that already exceeds WHOOP's number, the model
# reads too much into real (unrebuilt) heart rate.
REAL = []
for x in cyc:
    n = c.execute("SELECT COUNT(*) FROM ble_hr WHERE ts >= ? AND ts < ? AND hr > 0", (x["a"], x["b"])).fetchone()[0]
    if n > 0.3 * (x["b"] - x["a"]):
        REAL.append(x)
def real_check(p):
    t, ks, ka, s = p
    m = {"threshold_hrr": t, "active_scale": s, "rest_factor": ka}
    out = []
    for x in REAL:
        e = energy.energy(c, x["a"], x["b"], x["rest"], MAX_HR, body, energy.age_on(body, x["start"][:10]), m)
        out.append((x["start"][:10], x["kj"] / energy.KJ_PER_KCAL, e["kcal"], e["recorded_s"] / (x["b"] - x["a"])))
    return out

print("\nthreshold sweep, 'days (avg) + workouts, scale' fitted on the first 70%:")
sweep = []
for t in GRID:
    err, p = fit(split(DAYS["avg"])[0] + Wtr, t, "scale")
    rc = real_check(p)
    ok = all(model <= whoop for _, whoop, model, _ in rc)
    sweep.append((err, t, p, ok))
    real = "; ".join(f"{d} WHOOP {w:.0f} model at least {m:.0f} ({cv:.0%} recorded)" for d, w, m, cv in rc)
    print(f"   t={t:.2f} s={p[3]:.3f} train {100 * err:4.1f}%  days {report(split(DAYS['avg'])[1], p)}  "
          f"workouts {report(Wte, p)}  | {real}{'' if ok else '  << over WHOOP'}")
err, t_pick, p_pick, _ = min((r for r in sweep if r[3]), key=lambda r: r[0])
models.append((f"fit on days (avg) + workouts, scale, real-day check passed", p_pick))
# the frozen ENERGY_V1 numbers: that threshold, the scale refitted on every day and workout
FINAL = fit(DAYS["avg"] + W, t_pick, "scale")[1]
models.append(("ENERGY_V1: the same, scale refitted on all (in-sample)", FINAL))
print("ENERGY_V1 real days:", real_check(FINAL))
print()
out = []
for label, p in models:
    t, ks, ka, s = p
    print(f"{label}\n   t={t:.2f} asleep x{ks:.3f} awake x{ka:.3f} active x{s:.3f}")
    for mode in ("avg", "zones"):
        te = split(DAYS[mode])[1]
        print(f"   days ({mode:5s}) held-out ", report(te, p))
        print(f"   ... without days WHOOP put under resting ", report([x for x in te if x["bmr_ok"]], p))
    print("   workouts held-out     ", report(Wte, p))
    out.append({"label": label, "params": p})
json.dump({"cut": cut, "models": out}, open("/tmp/energy_fit.json", "w"), indent=1)

# ---------- no heart rate at all: training kcal per minute x length (the bar to beat) ----------
for name, (tr, te) in (("days (avg)", split(DAYS["avg"])), ("workouts", (Wtr, Wte))):
    rate = sum(x["kcal"] for x in tr) / sum(x["min"] for x in tr)
    e = np.array([rate * x["min"] - x["kcal"] for x in te]); y = np.array([x["kcal"] for x in te])
    print(f"no heart rate, {name:10s} held-out n={len(y):3d} MAE {np.abs(e).mean():4.0f} kcal "
          f"({100 * np.abs(e / y).mean():4.1f}%)  bias {e.mean():+5.0f}")

# ---------- drift: WHOOP's day energy by month vs the model ----------
print("\nby month (days, avg rebuild): WHOOP kcal, model bias, for 'fit on days (avg), scale'")
p = dict((m["label"], m["params"]) for m in out)["fit on days (avg), scale"]
by = {}
for x in DAYS["avg"]:
    by.setdefault(x["start"][:7], []).append(x)
for mo, L in sorted(by.items()):
    e, y = errs(L, p)
    print(f"   {mo}  n={len(L):2d}  WHOOP {y.mean():5.0f}  avg HR {np.mean([x['avg'] for x in L]):4.1f}  bias {e.mean():+5.0f}")
