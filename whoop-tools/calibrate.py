#!/usr/bin/env python3
"""Fit Strain, Sleep and Recovery to YOUR WHOOP archive, in one command, in plain Python.

If this database holds a WHOOP archive (the api_* tables, from whoop_api.py or import_data.py),
this fits the same things scores.py's v1 was fitted with (fit/v1/*.py, numpy + scipy), on your
days, with no extra packages:

  Strain    zone weights and the arctan curve on your workouts (WHOOP's zone minutes ->
            WHOOP's strain), the zone-0 share on your whole days, your max heart rate (WHOOP's
            figure if the profile has it, else the one your workouts' zones fit best).
  Sleep     your baseline need and sleep-debt carry-over (nights before May 2025, when WHOOP
            scored hours against need) and WHOOP's newer efficiency + consistency blend (nights
            from May 2025 on).
  Recovery  weights for resting HR, HRV and sleep against your WHOOP recoveries.

Each part is fitted on the first 70% of your days and tested on the last 30% (days the fit never
saw), next to generic-v1 on the same days. A part replaces generic-v1's only if its error there
is no larger. The result is stored under a new id (table score_models, never overwritten) and
selected for this install only if at least one part beats generic-v1. On the archive v1 was
fitted on, v1 stays selected unless you pass --select.

Run:  python calibrate.py                  # fit, report, store, select if better
      python calibrate.py --dry-run        # fit and report only, store nothing
      python calibrate.py --age 34         # save your age first (max heart rate when there is no archive)
      python calibrate.py --use generic-v1 # choose a model by hand (v1, generic-v1 or a fitted id)
      python calibrate.py --list           # the models this install knows
"""
import argparse, calendar, json, math, sys, time
import db, learn, scores

SPLIT = 0.7                     # fit on the first 70% of days, test on the rest
MIN_ROWS = 30                   # fewer archive rows than this: keep generic-v1 for that part
ZONES_FROM = "2024-12"          # WHOOP's zones are heart-rate-reserve zones from Dec 2024 (v1)
SLEEP_BLEND_FROM = "2025-05-08" # WHOOP blends efficiency and consistency into sleep performance
SLEEP_HOURS_UNTIL = "2025-05-01"  # ...before this, performance was hours against need
ZN = ["zone_zero_milli", "zone_one_milli", "zone_two_milli", "zone_three_milli", "zone_four_milli",
      "zone_five_milli"]


# ---------------- small numerics (no numpy) ----------------
def solve(A, b):
    """A x = b by Gaussian elimination with partial pivoting. None if singular."""
    n = len(b)
    M = [list(map(float, row)) + [float(v)] for row, v in zip(A, b)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        if abs(M[p][i]) < 1e-12:
            return None
        M[i], M[p] = M[p], M[i]
        for r in range(i + 1, n):
            f = M[r][i] / M[i][i]
            if f:
                M[r] = [a - f * b_ for a, b_ in zip(M[r], M[i])]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]
    return x


def lstsq(X, y):
    """Linear least squares (normal equations; the problems here have 2-4 columns)."""
    k = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) for j in range(k)] for i in range(k)]
    return solve(A, [sum(x[i] * v for x, v in zip(X, y)) for i in range(k)])


def lm(res, p0, iters=500):
    """Levenberg-Marquardt: the p minimising sum(res(p)^2), numeric Jacobian. Plays the part of
    scipy.optimize.least_squares in fit/v1."""
    p, lam = [float(v) for v in p0], 1e-3
    r = res(p)
    cost = sum(v * v for v in r)
    for _ in range(iters):
        J = []
        for j in range(len(p)):
            h = 1e-7 * max(1.0, abs(p[j]))
            q = p[:]
            q[j] += h
            J.append([(a - b) / h for a, b in zip(res(q), r)])
        A = [[sum(a * b for a, b in zip(J[i], J[j])) for j in range(len(p))] for i in range(len(p))]
        g = [sum(a * b for a, b in zip(J[i], r)) for i in range(len(p))]
        while True:
            M = [[A[i][j] * (1 + lam) + 1e-12 if i == j else A[i][j] for j in range(len(p))] for i in range(len(p))]
            d = solve(M, [-v for v in g])
            if d is not None:
                pn = [a + b for a, b in zip(p, d)]
                rn = res(pn)
                cn = sum(v * v for v in rn)
                if cn < cost:
                    break
            lam *= 10
            if lam > 1e12:
                return p
        gain = (cost - cn) / max(cost, 1e-300)
        p, r, cost, lam = pn, rn, cn, max(lam / 10, 1e-12)
        if gain < 1e-13:
            break
    return p


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def mae(pred, y):
    return mean(abs(a - b) for a, b in zip(pred, y))


def _t(s):
    """ISO time (UTC, as WHOOP writes it) -> unix seconds, keeping milliseconds."""
    t = calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    return t + (float("0" + s[19:23]) if len(s) > 19 and s[19] == "." else 0.0)


def _zone(v):
    return 2 if v >= 67 else 0 if v < 34 else 1


# ---------------- strain ----------------
def _weights(p):
    """fit/v1/strain.py kw(): zone 0 free, zone 1 = 1, zones 2-5 rising."""
    w, acc = [math.exp(p[2]), 1.0], 1.0
    for v in p[3:7]:
        acc += math.exp(v)
        w.append(acc)
    return w


def _curve(p, load):
    return 21 * 0.5 * (math.atan(math.exp(p[0]) * (math.log(max(load, 1e-6)) - p[1])) / (math.pi / 2) + 1)


def _as_model(p):
    return {"weights": [round(v, 5) for v in _weights(p)], "slope": round(math.exp(p[0]), 5), "offset": round(p[1], 5)}


def _model_strain(m, mins):
    return scores.strain_map(sum(a * b for a, b in zip(m["weights"], mins)), m)


def fit_strain(c, generic):
    W = []
    for w in c.execute("SELECT start, strain, zone_raw FROM api_workout WHERE zone_raw LIKE '%zone_zero%' "
                       "AND strain > 0 AND start >= ? ORDER BY start", (ZONES_FROM,)):
        z = json.loads(w["zone_raw"])
        mins = [(z.get(k) or 0) / 6e4 for k in ZN]
        if sum(mins[1:]) < 5 and w["strain"] > 8:        # logged by hand, no heart rate behind it
            continue
        W.append((mins, w["strain"]))
    out = {"workouts": len(W)}
    if len(W) < MIN_ROWS:
        out["skip"] = f"only {len(W)} workouts with WHOOP zone times (needs {MIN_ROWS})"
        return out
    n, cut = len(W), int(len(W) * SPLIT)
    p0 = [0, 4, -1, 1, 1, 1, 1]
    fit = lambda rows: lm(lambda p: [_curve(p, sum(a * b for a, b in zip(_weights(p), x))) - s for x, s in rows], p0)
    pt = fit(W[:cut])
    test = W[cut:]
    out["workout_mae"] = mae([_curve(pt, sum(a * b for a, b in zip(_weights(pt), x))) for x, _ in test], [s for _, s in test])
    out["generic_workout_mae"] = mae([_model_strain(generic, x) for x, _ in test], [s for _, s in test])
    out["workouts_tested"] = len(test)
    pa = fit(W)
    out["params"] = _as_model(pa)
    k = out["params"]["weights"]

    # whole days: the share of minutes under zone 1 that counts as zone 0, by the day's average HR
    allw = [(_t(w["start"]), [(json.loads(w["zone_raw"]).get(z) or 0) / 6e4 for z in ZN])
            for w in c.execute("SELECT start, zone_raw FROM api_workout WHERE zone_raw LIKE '%zone_zero%'")]
    D = []
    for cy in c.execute("SELECT start, end, strain, avg_hr FROM api_cycle WHERE end IS NOT NULL AND strain > 0 "
                        "AND start >= ? AND avg_hr > 0 ORDER BY start", (ZONES_FROM,)):
        a, b = _t(cy["start"]), _t(cy["end"])
        z = [0.0] * 6
        for t, x in allw:
            if a <= t < b:
                z = [u + v for u, v in zip(z, x)]
        D.append((cy["strain"], z, (b - a) / 60 - sum(z), cy["avg_hr"]))

    def day(q, d, m):
        s, z, rest, avg = d
        share = min(1.0, max(0.0, q[0] + q[1] * (avg - 70)))
        return scores.strain_map(sum(a * b for a, b in zip(m["weights"][1:], z[1:])) + (z[0] + rest * share) * m["weights"][0], m)

    out["days"] = len(D)
    if len(D) >= MIN_ROWS:
        cd = int(len(D) * SPLIT)
        m = out["params"]
        qt = lm(lambda q: [day(q, d, m) - d[0] for d in D[:cd]], [0.6, 0.02])
        out["day_mae"] = mae([day(qt, d, m) for d in D[cd:]], [d[0] for d in D[cd:]])
        out["generic_day_mae"] = mae([day(generic["zone0_share"], d, generic) for d in D[cd:]], [d[0] for d in D[cd:]])
        out["days_tested"] = len(D) - cd
        qa = lm(lambda q: [day(q, d, m) - d[0] for d in D], [0.6, 0.02])
        out["params"]["zone0_share"] = [round(v, 4) for v in qa]
    else:
        out["params"]["zone0_share"] = list(generic["zone0_share"])
    out["params"]["max_hr"], out["max_hr_from"], out["max_hr_zone_match"] = fit_max_hr(c)
    out["params"]["rest_default"] = generic["rest_default"]
    # generic-v1's max HR (age formula or the strap's highest) on the same workouts
    rows, rest = _max_hr_rows(c)
    gm, out["generic_max_hr_from"] = scores.personal_max_hr(c, generic)
    out["generic_max_hr"] = gm
    out["generic_max_hr_zone_match"] = _zone_match(rows, rest, gm) if rows else None
    return out


def _zone_match(rows, rest, M):
    """fit/v1/zones.py: share of workouts whose max HR fits the top zone they reached, for max HR M."""
    def ok(m, k):
        f = lambda p: rest + p * (M - rest)
        lo = f(0.4 + 0.1 * k) if k else 0
        hi = f(0.5 + 0.1 * k) if k < 5 else 1e9
        return lo - 1 <= m < hi + 1
    return mean(ok(m, k) for m, k in rows)


def _max_hr_rows(c):
    """(workout max HR, top zone reached) for the archive's last 90 days, and resting HR then."""
    last = c.execute("SELECT MAX(start) s FROM api_workout WHERE zone_raw LIKE '%zone_zero%' AND max_hr > 0").fetchone()["s"]
    if not last:
        return [], None
    since = time.strftime("%Y-%m-%d", time.gmtime(_t(last) - 90 * 86400))
    rows = []
    for r in c.execute("SELECT max_hr, zone_raw FROM api_workout WHERE zone_raw LIKE '%zone_zero%' AND max_hr > 0 "
                       "AND start >= ?", (since,)):
        z = json.loads(r["zone_raw"])
        top = max([i for i in range(6) if (z.get(ZN[i]) or 0) > 0], default=0)
        rows.append((r["max_hr"], top))
    rhr = [r["resting_hr"] for r in c.execute("SELECT resting_hr FROM api_recovery WHERE resting_hr > 0 "
                                               "AND created_at <= ? ORDER BY created_at DESC LIMIT 30", (last,))]
    return rows, learn._median(rhr) if rhr else 50.0


def fit_max_hr(c):
    """(max HR, where from, zone match): WHOOP's figure from the profile, else the max HR whose
    heart-rate-reserve zones best explain your last 90 days of workouts (ties: the higher)."""
    rows, rest = _max_hr_rows(c)
    r = c.execute("SELECT value FROM api_profile WHERE key='max_heart_rate'").fetchone()
    try:
        if r and float(r["value"]) > 120:
            v = float(r["value"])
            return v, "WHOOP's figure (profile)", _zone_match(rows, rest, v) if rows else None
    except ValueError:
        pass
    if len(rows) < 10:
        return None, "too few workouts with zones: yours from the strap, as generic-v1", None
    best = max((_zone_match(rows, rest, M), M) for M in range(160, 221))
    return float(best[1]), f"best fit to {len(rows)} workouts' zones", best[0]


# ---------------- sleep ----------------
def _sleep_rows(c):
    """fit/v1/sleep.py rows: each main sleep with the strain of the cycle that ended at its start."""
    S = c.execute("SELECT * FROM api_sleep WHERE nap=0 AND sleep_perf IS NOT NULL AND total_in_bed_ms > 0 "
                  "ORDER BY start").fetchall()
    N = [(_t(r["start"]), (r["total_in_bed_ms"] - (r["total_awake_ms"] or 0)) / 36e5)
         for r in c.execute("SELECT * FROM api_sleep WHERE nap=1 AND total_in_bed_ms > 0")]
    C = [(_t(r["start"]), _t(r["end"]), r["strain"]) for r in
         c.execute("SELECT start, end, strain FROM api_cycle WHERE end IS NOT NULL AND strain > 0")]
    rows = []
    for i, s in enumerate(S):
        t = _t(s["start"])
        prev = [x for x in C if abs(x[1] - t) < 1800]
        if not prev or i == 0:
            continue
        rows.append({"d": s["start"][:10], "perf": s["sleep_perf"], "strain": prev[0][2],
                     "asleep": (s["total_in_bed_ms"] - (s["total_awake_ms"] or 0)) / 36e5,
                     "nap": sum(h for nt, h in N if prev[0][0] <= nt < t),
                     "eff": s["sleep_efficiency"], "cons": s["sleep_consistency"]})
    return rows


def _hours(base, carry, R, bases=None):
    """Sleep performance as hours against need, with last night's shortfall carried over."""
    out, debt = [], 0.0
    for i, x in enumerate(R):
        need = (bases[i] if bases else base) + scores.need_from_strain(x["strain"]) + debt - x["nap"]
        out.append(min(100.0, 100 * x["asleep"] / need))
        debt = carry * max(0.0, need - x["asleep"])
    return out


def _generic_bases(rows, generic):
    """generic-v1's baseline for each night: the median of the nights before it, as at runtime."""
    lo, hi = scores.GATES["sleep_h"]
    a, b = generic["baseline_range_h"]
    out, seen = [], []
    for x in rows:
        last = seen[-30:]
        out.append(min(b, max(a, learn._median(last))) if len(last) >= generic["baseline_min_nights"]
                   else generic["baseline_default_h"])
        if lo <= x["asleep"] <= hi:
            seen.append(x["asleep"])
    return out


def fit_sleep(c, generic):
    rows = _sleep_rows(c)
    gb = _generic_bases(rows, generic)
    for x, b in zip(rows, gb):
        x["gbase"] = b
    pre = [x for x in rows if x["d"] < SLEEP_HOURS_UNTIL]
    post = [x for x in rows if x["d"] >= SLEEP_BLEND_FROM]
    out = {"nights_hours": len(pre), "nights_blend": len(post)}
    if len(pre) < MIN_ROWS:
        out["skip"] = f"only {len(pre)} nights scored as hours against need (before May 2025; needs {MIN_ROWS})"
        return out
    y = [x["perf"] for x in pre]
    cut = int(len(pre) * SPLIT)
    pt = lm(lambda p: [a - b for a, b in zip(_hours(p[0], p[1], pre)[:cut], y[:cut])], [8, 0.3])
    out["hours_mae"] = mae(_hours(pt[0], pt[1], pre)[cut:], y[cut:])
    out["generic_hours_mae"] = mae(_hours(None, generic["debt_carry"], pre, [x["gbase"] for x in pre])[cut:], y[cut:])
    out["hours_tested"] = len(pre) - cut
    pa = lm(lambda p: [a - b for a, b in zip(_hours(p[0], p[1], pre), y)], [8, 0.3])
    params = {"baseline_h": round(pa[0], 3), "debt_carry": round(pa[1], 3), "blend": None,
              "blend_no_consistency": None, "consistency_scale": list(generic["consistency_scale"])}
    out["error"], out["generic_error"], out["error_on"] = out["hours_mae"], out["generic_hours_mae"], "hours vs need"

    # since May 2025: perf = a*hours + b*efficiency + c*consistency + d
    hp = _hours(pa[0], pa[1], post)
    gp = _hours(None, generic["debt_carry"], post, [x["gbase"] for x in post])
    yp = [x["perf"] for x in post]
    idx = [i for i, x in enumerate(post) if x["cons"] is not None and x["eff"] is not None]
    if len(idx) >= MIN_ROWS:
        X = {i: [hp[i], post[i]["eff"], post[i]["cons"], 1.0] for i in idx}
        ct = int(len(idx) * SPLIT)
        tr, te = idx[:ct], idx[ct:]
        w = lstsq([X[i] for i in tr], [yp[i] for i in tr])
        out["blend_mae"] = mae([sum(a * b for a, b in zip(w, X[i])) for i in te], [yp[i] for i in te])
        out["generic_blend_mae"] = mae([gp[i] for i in te], [yp[i] for i in te])
        out["blend_tested"] = len(te)
        wa = lstsq([X[i] for i in idx], [yp[i] for i in idx])
        ie = [i for i, x in enumerate(post) if x["eff"] is not None]
        X2 = {i: [hp[i], post[i]["eff"], 1.0] for i in ie}
        c2 = int(len(ie) * SPLIT)
        w2t = lstsq([X2[i] for i in ie[:c2]], [yp[i] for i in ie[:c2]])
        out["no_consistency_mae"] = mae([sum(a * b for a, b in zip(w2t, X2[i])) for i in ie[c2:]], [yp[i] for i in ie[c2:]])
        w2 = lstsq([X2[i] for i in ie], [yp[i] for i in ie])
        params["blend"] = [round(v, 3) for v in wa]
        params["blend_no_consistency"] = [round(v, 3) for v in w2]
        out["error"], out["generic_error"], out["error_on"] = out["blend_mae"], out["generic_blend_mae"], "WHOOP's blend (since May 2025)"
        sc = fit_consistency(c)
        if sc:
            params["consistency_scale"] = sc["scale"]
            out.update(consistency_mae=sc["mae"], generic_consistency_mae=sc["raw_mae"], consistency_nights=sc["n"])
    out["params"] = params
    return out


def fit_consistency(c):
    """fit/v1/consistency.py "all4": share of 5-minute slots in the same state on each of the 4
    days before each wake, against WHOOP's consistency, linear (in-sample, as v1)."""
    allS = [(_t(r["start"]), _t(r["end"])) for r in c.execute("SELECT start, end FROM api_sleep WHERE end IS NOT NULL")]
    pts = []
    for r in c.execute("SELECT end, sleep_consistency FROM api_sleep WHERE nap=0 AND sleep_consistency IS NOT NULL "
                       "AND end IS NOT NULL ORDER BY start"):
        end = _t(r["end"])
        spans = [(a, b) for a, b in allS if b > end - 4 * 86400 and a < end]
        grid = [[any(a <= end - (d + 1) * 86400 + k * 60 < b for a, b in spans) for k in range(0, 1440, 5)]
                for d in range(4)]
        same = mean(all(grid[d][k] == grid[0][k] for d in range(4)) for k in range(len(grid[0])))
        pts.append((100 * same, r["sleep_consistency"]))
    if len(pts) < MIN_ROWS:
        return None
    a, b = lstsq([[v, 1.0] for v, _ in pts], [w for _, w in pts])
    return {"scale": [round(a, 3), round(b, 3)], "n": len(pts),
            "mae": mae([a * v + b for v, _ in pts], [w for _, w in pts]), "raw_mae": mae([v for v, _ in pts], [w for _, w in pts])}


# ---------------- recovery ----------------
def _recovery_rows(c):
    """fit/v1/recovery.py features: resting HR and HRV against the last 30 days' median, in units
    of their spread, and sleep performance."""
    R = [dict(r) for r in c.execute(
        "SELECT r.created_at, r.recovery_score y, r.resting_hr rhr, r.hrv_rmssd_ms hrv, s.sleep_perf sp, s.resp_rate rr "
        "FROM api_recovery r LEFT JOIN api_sleep s ON s.id = r.sleep_id WHERE r.recovery_score IS NOT NULL "
        "AND r.resting_hr > 0 AND r.hrv_rmssd_ms > 0 ORDER BY r.created_at")]
    med = learn._median

    def spread(x, fl, de, ce):
        if len(x) < 5:
            return de
        m = med(x)
        return min(ce, max(fl, 1.4826 * med([abs(v - m) for v in x])))
    F = []
    for i, x in enumerate(R):
        t = _t(x["created_at"])
        prev = [p for p in R[:i] if t - _t(p["created_at"]) <= 30 * 86400]
        if len(prev) < 10 or x["sp"] is None or x["rr"] is None:
            continue
        rh = [p["rhr"] for p in prev]
        hv = [math.log(p["hrv"]) for p in prev]
        F.append((x["y"], (x["rhr"] - med(rh)) / spread(rh, *learn.RHR_SPREAD),
                  (math.log(x["hrv"]) - med(hv)) / spread(hv, *learn.HRV_SPREAD), (x["sp"] - 80) / 10))
    return F


def fit_recovery(c, generic):
    F = _recovery_rows(c)
    out = {"days": len(F)}
    if len(F) < MIN_ROWS:
        out["skip"] = f"only {len(F)} WHOOP recoveries with 10 nights before them (needs {MIN_ROWS})"
        return out
    cut = int(len(F) * SPLIT)
    test = F[cut:]
    y = [f[0] for f in test]
    clip = lambda v: min(99.0, max(1.0, v))
    res = {}
    for name, cols in (("with_sleep", (1, 2, 3)), ("without_sleep", (1, 2))):
        X = lambda f: [1.0] + [f[k] for k in cols]
        w = lstsq([X(f) for f in F[:cut]], [f[0] for f in F[:cut]])
        pred = [clip(sum(a * b for a, b in zip(w, X(f)))) for f in test]
        gpred = [clip(sum(a * b for a, b in zip(generic[name], X(f)))) for f in test]
        res[name] = (mae(pred, y), mean(_zone(a) == _zone(b) for a, b in zip(pred, y)),
                     mae(gpred, y), mean(_zone(a) == _zone(b) for a, b in zip(gpred, y)),
                     [round(v, 3) for v in lstsq([X(f) for f in F], [f[0] for f in F])])
    out.update(mae=res["with_sleep"][0], zone_match=res["with_sleep"][1],
               generic_mae=res["with_sleep"][2], generic_zone_match=res["with_sleep"][3],
               mae_without_sleep=res["without_sleep"][0], zone_match_without_sleep=res["without_sleep"][1],
               generic_mae_without_sleep=res["without_sleep"][2], tested=len(test),
               error=res["with_sleep"][0], generic_error=res["with_sleep"][2])
    out["params"] = {"with_sleep": res["with_sleep"][4], "without_sleep": res["without_sleep"][4]}
    return out


# ---------------- the whole calibration ----------------
def has_archive(c):
    return scores.fingerprint(c) is not None


def calibrate(c):
    """Fit every part; returns (params, validation). Parts that don't beat generic-v1 on the
    held-out days (or lack data) are generic-v1's."""
    g = scores.MODELS[scores.GENERIC]
    fits = {"strain": fit_strain(c, g["strain"]), "sleep": fit_sleep(c, g["sleep"]),
            "recovery": fit_recovery(c, g["recovery"])}
    errs = {"strain": ("workout_mae", "generic_workout_mae")}
    params, used, better = {}, [], []
    for part, f in fits.items():
        e, ge = (f.get(errs[part][0]), f.get(errs[part][1])) if part in errs else (f.get("error"), f.get("generic_error"))
        if "params" in f and e is not None and ge is not None and e <= ge + 1e-9:
            # a fitted part takes generic-v1's place; fields it doesn't fit stay generic
            params[part] = dict(g[part], **{k: v for k, v in f["params"].items() if v is not None})
            params[part]["validation"] = {"fitted_to_you": True, **{k: (round(v, 3) if isinstance(v, float) else v)
                                                                   for k, v in f.items() if k != "params"}}
            used.append(part)
            if e < ge - 1e-9:
                better.append(part)
        else:
            params[part] = dict(g[part])
            why = f.get("skip") or (f"the fit did worse than generic-v1 on held-out days ({e:.2f} vs {ge:.2f})"
                                    if e is not None and ge is not None else "not enough data")
            params[part]["validation"] = dict(g[part]["validation"], why=why)
    # Max heart rate is judged on its own (the strain errors above use WHOOP's zone minutes, so
    # they don't see it): WHOOP's figure, or the one that explains your zones at least as well
    # as generic-v1's age formula / strap maximum, replaces the generic one.
    s = fits["strain"]
    mh, zm, gzm = (s.get("params") or {}).get("max_hr"), s.get("max_hr_zone_match"), s.get("generic_max_hr_zone_match")
    if mh and "strain" not in used and (zm is None or gzm is None or zm >= gzm):
        params["strain"]["max_hr"] = mh
        params["strain"]["validation"]["max_hr"] = {"fitted_to_you": True, "from": s["max_hr_from"],
                                                    "zone_match": zm, "generic_zone_match": gzm}
        used.append("strain max heart rate")
        if zm is None or gzm is None or zm > gzm:
            better.append("strain max heart rate")
    validation = {"parts_fitted": used, "parts_better_than_generic": better,
                  "fits": {k: {a: b for a, b in f.items() if a != "params"} for k, f in fits.items()}}
    return params, validation, fits


def new_id(c):
    base = "fit-" + time.strftime("%Y-%m-%d")
    c.execute(scores.MODELS_SCHEMA)
    mid, n = base, 1
    while scores.get_model(c, mid):
        n += 1
        mid = f"{base}-{n}"
    return mid


def _f(v, fmt="{:.2f}"):
    return "-" if v is None else fmt.format(v)


def report(fits, validation, say=print):
    s, sl, r = fits["strain"], fits["sleep"], fits["recovery"]
    say("Held-out days (the last 30%, which the fit never saw): mean error")
    say(f"  {'':34s} {'fitted':>8s} {'generic-v1':>11s} {'days':>5s}")
    lines = [("Strain, workouts (0-21)", s.get("workout_mae"), s.get("generic_workout_mae"), s.get("workouts_tested")),
             ("Strain, whole days (0-21)", s.get("day_mae"), s.get("generic_day_mae"), s.get("days_tested")),
             ("Sleep, hours vs need (points)", sl.get("hours_mae"), sl.get("generic_hours_mae"), sl.get("hours_tested")),
             ("Sleep, WHOOP's 2025 blend (points)", sl.get("blend_mae"), sl.get("generic_blend_mae"), sl.get("blend_tested")),
             ("Recovery (points)", r.get("mae"), r.get("generic_mae"), r.get("tested")),
             ("Recovery without sleep (points)", r.get("mae_without_sleep"), r.get("generic_mae_without_sleep"), r.get("tested"))]
    for name, a, b, n in lines:
        if a is not None or b is not None:
            say(f"  {name:34s} {_f(a):>8s} {_f(b):>11s} {_f(n, '{}'):>5s}")
    if r.get("zone_match") is not None:
        say(f"  Recovery in the right zone: {r['zone_match']:.0%} fitted, {r['generic_zone_match']:.0%} generic-v1")
    if sl.get("consistency_mae") is not None:
        say(f"  Sleep consistency (all nights, as v1): {sl['consistency_mae']:.1f} fitted scale, "
            f"{sl['generic_consistency_mae']:.1f} unscaled")
    if s.get("params", {}).get("max_hr"):
        zm, gzm = s.get("max_hr_zone_match"), s.get("generic_max_hr_zone_match")
        say(f"  Max heart rate: fitted {s['params']['max_hr']:.0f} ({s['max_hr_from']}"
            + (f"; zones fit {zm:.0%} of recent workouts)" if zm is not None else ")"))
        say(f"                  generic-v1 {s['generic_max_hr']:.0f} ({s['generic_max_hr_from']}"
            + (f"; zones fit {gzm:.0%})" if gzm is not None else ")"))
    for part, f in fits.items():
        if f.get("skip"):
            say(f"  {part}: not fitted, {f['skip']}")
    say("Parts taken from the fit: " + (", ".join(validation["parts_fitted"]) or "none")
        + "; better than generic-v1: " + (", ".join(validation["parts_better_than_generic"]) or "none"))


def compare_v1(params, say=print):
    """How close the plain-Python fit is to the numbers in MODELS["v1"] (for the owner's archive)."""
    v1 = scores.MODELS[scores.OWNER]
    pairs = [("strain", k) for k in ("max_hr", "weights", "slope", "offset", "zone0_share")] + \
            [("sleep", k) for k in ("baseline_h", "debt_carry", "blend", "blend_no_consistency", "consistency_scale")] + \
            [("recovery", k) for k in ("with_sleep", "without_sleep")]
    say("Compared with v1 (fit/v1, numpy + scipy):")
    for part, k in pairs:
        a, b = params[part].get(k), v1[part].get(k)
        if isinstance(a, list) and isinstance(b, list):
            d = max(abs(x - y) for x, y in zip(a, b))
        elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
            d = abs(a - b)
        else:
            d = None
        say(f"  {part}.{k}: fitted {a}  v1 {b}  largest difference {_f(d, '{:.4g}')}")


def backfill(c, mid, say=print):
    start = scores.first_day(c)
    if not start:
        return
    days = min(400, int((time.time() - scores._midnight(start)) / 86400) + 1)
    out = scores.update(c, days=days, version=mid)
    say(f"Recomputed the scores of the last {len(out)} days with {mid}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fit Strain, Sleep and Recovery to your WHOOP archive.")
    ap.add_argument("--dry-run", action="store_true", help="fit and report, store nothing")
    ap.add_argument("--select", action="store_true", help="select the new model even on v1's own archive")
    ap.add_argument("--age", type=float, help="save your age in the local profile (for max heart rate)")
    ap.add_argument("--use", metavar="ID", help="choose the model this install uses")
    ap.add_argument("--list", action="store_true", help="list the models this install knows")
    ap.add_argument("--compare-v1", action="store_true", help="show how close the fit is to v1's numbers")
    a = ap.parse_args(argv)
    c = db.connect()
    try:
        run(a, c)
    finally:
        c.close()


def run(a, c):
    c.execute(scores.MODELS_SCHEMA)
    if a.age is not None:
        if not 10 <= a.age <= 100:
            sys.exit("age must be between 10 and 100")
        db.upsert(c, "api_profile", {"key": "age", "value": str(a.age)})
        c.commit()
        print(f"Saved age {a.age:g}: generic-v1's max heart rate is now 208 - 0.7 x age = {208 - 0.7 * a.age:.0f} "
              "(or higher if the strap sees it).")
        if not (a.use or a.list or a.dry_run or a.select or a.compare_v1):
            return
    if a.list:
        cur = scores.current(c)
        ids = list(scores.MODELS) + [r["id"] for r in c.execute("SELECT id FROM score_models ORDER BY fitted_at")]
        for mid in ids:
            print(("* " if mid == cur else "  ") + mid)
        print("now using:", "%s (%s)" % scores.which(c))
        return
    if a.use:
        if not scores.get_model(c, a.use):
            sys.exit(f"no model {a.use}; see --list")
        db.set_state(c, "scores_model", a.use)
        print(f"This install now uses {a.use}.")
        backfill(c, a.use)
        return
    if not has_archive(c):
        print("No WHOOP archive in this database (the api_* tables are empty), so there is nothing to fit.\n"
              "Scores use generic-v1: published figures and your own nights, not fitted to you.\n"
              "To fit: pull your WHOOP history (python whoop_api.py login, then sync) or load an export "
              "(python import_data.py), then run this again.")
        return
    params, validation, fits = calibrate(c)
    fp = scores.fingerprint(c)
    print(f"WHOOP archive up to {fp['until']}.")
    report(fits, validation)
    if scores.fitted_here(c, scores.MODELS[scores.OWNER].get("fingerprint")):
        print("Note: this is the archive v1 was fitted on, and generic-v1 reuses v1's strain numbers\n"
              "(its recovery weights were also chosen with v1's fit in view), so generic-v1 has seen these\n"
              "test days: its errors here look better than they would for anyone else.")
    if a.compare_v1:
        compare_v1({k: dict(v, **(fits[k].get("params") or {})) for k, v in params.items()})
    if a.dry_run:
        print("Dry run: nothing stored.")
        return
    mid = new_id(c)
    scores.save_model(c, mid, params, validation, fp)
    print(f"Stored as {mid}.")
    if not validation["parts_better_than_generic"]:
        print(f"Not selected: it doesn't beat generic-v1 on any score. This install keeps {scores.current(c)}.")
        return
    if scores.current(c) == scores.OWNER and not a.select:
        print("Not selected: this database is the archive v1 was fitted on, so it keeps v1. "
              f"Use `python calibrate.py --use {mid}` to switch.")
        return
    db.set_state(c, "scores_model", mid)
    print(f"Selected: this install now uses {mid}.")
    backfill(c, mid)


if __name__ == "__main__":
    main()
