# Chooses metrics.RESP (breathing rate from beat intervals) on half the subjects and reports
# its accuracy on the other half. Run prepare.py first (see its header), then from whoop-tools/:
#   /tmp/fitenv/bin/python -I fit/resp_rate/fit.py /tmp/resp-prep
# Everything goes through the shipped pure-Python code in metrics.py (beat_series, calm_beats,
# breath_spectrum, breath_peak, resp_rate) on an in-memory ble_hr table, so the numbers are
# for exactly what runs on the strap data. Takes ~35 minutes (pure-Python spectra).
# Result on 2026-11-19 (frozen as scores.RESP_RATE["r1"]): calm_margin 20, diff, 120 s windows,
# 0.14-0.4 Hz, min_peak 0.25, min_cover 0.8, min_windows 10; held-out window MAE 0.53,
# recording MAE 1.13 (median 0.49), 25 of 29 recordings with a value.
#
# Split: by subject (both nights of an SLPDB subject on the same side), alternating in name
# order within each dataset. Parameters are picked on "fit": lowest recording-level error
# among the settings that give a value for at least MIN_SHARE of the recordings. The held-out
# ("test") subjects are scored once, with the chosen settings, end to end via resp_rate.
#
# Errors are breaths/min. Window level: each accepted window's peak vs the reference count on
# the same window. Recording level ("night"): resp_rate's median vs the median of the reference
# over the same windows with beats.
import json, os, sqlite3, statistics, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import metrics  # noqa: E402

WIDE = (0.08, 0.6)
VARIANTS = [(m, d) for m in (10, 20) for d in (False, True)]   # (calm_margin, diff)
WINDOWS = (60, 120)
BANDS = [(lo, hi) for lo in (0.1, 0.12, 0.14, 0.16) for hi in (0.4, 0.5)]
PEAKS = (0.0, 0.1, 0.15, 0.2, 0.25, 0.3)
COVERS = (0.5, 0.65, 0.8)
MIN_WINDOWS = (10, 20)
MIN_SHARE = 0.7


def load(path):
    out = []
    for f in sorted(os.listdir(path)):
        if f.endswith(".json"):
            with open(os.path.join(path, f)) as fh:
                out.append(json.load(fh))
    subjects = {}
    for ds in ("fantasia", "slpdb"):
        names = sorted({r["subject"] for r in out if r["dataset"] == ds})
        subjects.update({s: ("fit" if i % 2 == 0 else "test") for i, s in enumerate(names)})
    for r in out:
        r["split"] = subjects[r["subject"]]
    return out


def strap_db(rec):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE ble_hr (ts REAL PRIMARY KEY, hr INTEGER, rr_ms TEXT)")
    c.executemany("INSERT INTO ble_hr VALUES (?,?,?)", rec["rows"])
    return c, rec["rows"][-1][0] + 1


def windows(rec, w, margin, diff):
    """Per window with calm beats: (start, coverage, (freqs, power) over WIDE or None)."""
    c, b = strap_db(rec)
    p = dict(metrics.RESP, window_s=w, band=WIDE, min_cover=0.0, calm_margin=margin, diff=diff)
    out = []
    beats = metrics.calm_beats(c, 0.0, b, p)
    t, i0 = 0.0, 0
    while t + w <= b:
        while i0 < len(beats) and beats[i0][0] < t:
            i0 += 1
        sub = [x for x in beats[i0:i0 + w * 4] if x[0] < t + w]
        if sub:
            out.append((int(t), sum(x for _, x in sub) / 1000.0 / w, metrics.breath_spectrum(sub, t, p)))
        t += p["step_s"]
    return out


def peaks(wins):
    """{band: [(start, coverage, (breaths/min, peak share) or None)]}."""
    out = {}
    for band in BANDS:
        rows = []
        for t, cov, sp in wins:
            e = None
            if sp:
                keep = [i for i, f in enumerate(sp[0]) if band[0] - 1e-9 <= f <= band[1] + 1e-9]
                e = metrics.breath_peak([sp[0][i] for i in keep], [sp[1][i] for i in keep], metrics.RESP["df"])
            rows.append((t, cov, e))
        out[band] = rows
    return out


def evaluate(recs, cache, key, band, min_peak, min_cover, min_windows):
    w = key[2]
    werr, nerr, nights, missing = [], [], 0, 0
    for r in recs:
        ref = r["ref"][str(w)]
        rates, refs = [], []
        for t, cov, e in cache[r["record"]][key][band]:
            if str(t) in ref:
                refs.append(ref[str(t)])
            if e is None or cov < min_cover or e[1] < min_peak:
                continue
            rates.append(e[0])
            if str(t) in ref:
                werr.append(abs(e[0] - ref[str(t)]))
        if len(refs) < 10:
            continue                       # no usable reference for this recording
        nights += 1
        if len(rates) < min_windows:
            missing += 1
            continue
        nerr.append(abs(statistics.median(rates) - statistics.median(refs)))
    mean = lambda x: sum(x) / len(x) if x else float("nan")
    return {"window_mae": mean(werr), "windows": len(werr), "night_mae": mean(nerr),
            "night_median_err": statistics.median(nerr) if nerr else float("nan"),
            "within1": mean([e <= 1 for e in nerr]), "within2": mean([e <= 2 for e in nerr]),
            "nights": nights, "with_value": nights - missing}


def show(e):
    return {k: round(v, 2) for k, v in e.items()}


def main(path):
    recs = load(path)
    cache = {r["record"]: {} for r in recs}
    for m, d in VARIANTS:
        for w in WINDOWS:
            for r in recs:
                cache[r["record"]][(m, d, w)] = peaks(windows(r, w, m, d))
            print("spectra done", m, d, w, file=sys.stderr, flush=True)
    fit = [r for r in recs if r["split"] == "fit"]
    test = [r for r in recs if r["split"] == "test"]
    print(f"{len(fit)} fit recordings, {len(test)} held-out "
          f"({sum(r['dataset'] == 'slpdb' for r in test)} of them sleep recordings)")
    grid = []
    for key in cache[fit[0]["record"]]:
        for band in BANDS:
            for mp in PEAKS:
                for cv in COVERS:
                    for mw in MIN_WINDOWS:
                        e = evaluate(fit, cache, key, band, mp, cv, mw)
                        if e["with_value"] >= MIN_SHARE * e["nights"]:
                            grid.append((e["night_mae"], key, band, mp, cv, mw, e))
    grid.sort(key=lambda g: g[0])
    print(f"best on the fit half (value for >= {MIN_SHARE:.0%} of recordings); key = (calm_margin, diff, window_s):")
    for g in grid[:15]:
        print(" ", g[1:6], show(g[6]))
    _, key, band, mp, cv, mw, _ = grid[0]
    chosen = dict(calm_margin=key[0], diff=key[1], window_s=key[2], band=band, min_peak=mp, min_cover=cv,
                  min_windows=mw)
    print("\nchosen:", chosen)
    for name, rs in (("fit", fit), ("test", test)):
        for ds in ("fantasia", "slpdb", None):
            sub = [r for r in rs if ds is None or r["dataset"] == ds]
            print(f"  {name:4s} {ds or 'all':8s}", show(evaluate(sub, cache, key, band, mp, cv, mw)))
    print("\nheld-out, plain choice for comparison (margin 10, no diff, 120 s, 0.12-0.4 Hz, no gates):")
    print("  ", show(evaluate(test, cache, (10, False, 120), (0.12, 0.4), 0.0, 0.5, 10)))

    # end to end through metrics.resp_rate with the chosen parameters, held-out recordings
    p = dict(metrics.RESP, **chosen)
    print("\nheld-out, metrics.resp_rate end to end (estimate vs reference median):")
    errs = {}
    for r in test:
        c, b = strap_db(r)
        out = metrics.resp_rate(c, 0.0, b, p)
        ref = r["ref"][str(p["window_s"])]
        wins = metrics.breath_windows(metrics.calm_beats(c, 0.0, b, p), 0.0, b, p)
        refs = [ref[str(int(t))] for t, _ in wins if str(int(t)) in ref]
        rm = statistics.median(refs) if len(refs) >= 10 else None
        if out["bpm"] is not None and rm is not None:
            errs.setdefault(r["dataset"], []).append(abs(out["bpm"] - rm))
        print(f"  {r['record']:7s} est {out['bpm']}  ref {rm and round(rm, 1)}  windows {out['windows']}/{out['of']}")
    for ds, e in errs.items():
        print(f"  {ds}: MAE {sum(e) / len(e):.2f}, median {statistics.median(e):.2f}, within 1: "
              f"{sum(x <= 1 for x in e)}/{len(e)}, within 2: {sum(x <= 2 for x in e)}/{len(e)}")


if __name__ == "__main__":
    main(sys.argv[1])
