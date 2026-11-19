# Check sleep.py against a sleep lab (polysomnography). Pure Python; runs the real sleep.classify and
# sleep.find, only swapping their constants. Input: the JSON made by convert.py (see its header for the
# dataset and how to rebuild it). Rerun, from whoop-tools/:
#   python3 -I fit/sleep_psg/validate.py /tmp/psg.json
# Prints, per person and summed up: sleep.py's constants now vs the ones before this check (HR_ABOVE 15,
# JOIN 30), then a leave-one-subject-out choice of HR_ABOVE x JOIN (each person scored with the setting
# that worked best on the other 30), which is the held-out number quoted in sleep.py's docstring.
#
# What is compared (minutes; + = later / more than the lab):
#   onset       sleep.py's start of the night vs the lab's first sleep epoch
#   final wake  sleep.py's end of the night vs the end of the lab's last sleep epoch
#   TST         minutes asleep inside the night vs the lab's sleep epochs
#   accuracy    per 30 s lab epoch (unscored epochs skipped): asleep = inside the night and that minute 's'
#   sensitivity share of lab sleep called asleep; specificity: share of lab wake called awake
#   wake-ups    awake runs of WAKEUP+ minutes inside the night vs lab wake runs of 5+ minutes
#
# Also tried on the same data and left out (2026-11-19): tuning STILL (best 0.025 on the instantaneous
# 1 Hz signal but 0.015 on the smoothed one, so it is device-dependent and stays 0.02); a threshold
# relative to the night's quietest minutes (good on one signal, far worse on the other); FILL 3-5
# (no consistent gain); a van Hees-style posture rule (wrist direction turning > 5-30 degrees within
# +-2 min means awake: finds more wake but loses much more sleep, worse at every angle tried).
import json, os, statistics, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import sleep  # noqa: E402

BEFORE = {"HR_ABOVE": 15, "JOIN": 30}
GRID = [{"HR_ABOVE": h, "JOIN": j} for h in (15, 20, 25, 30, 40) for j in (30, 45, 60, 90)]


def with_constants(consts, fn):
    old = {k: getattr(sleep, k) for k in consts}
    for k, v in consts.items():
        setattr(sleep, k, v)
    try:
        return fn()
    finally:
        for k, v in old.items():
            setattr(sleep, k, v)


def score(s):
    """One person, with sleep.py's current constants."""
    a, ep = s["a"], s["epochs"]
    st = sleep.classify(s["move"], s["hr"])
    found = sleep.find(st)
    sl = [t for t, v in ep if v > 0]
    on, off = sl[0], sl[-1] + 30
    lab_wakeups, run = 0, 0
    for t, v in ep:
        if on <= t < off:
            run = run + 1 if v == 0 else 0
            lab_wakeups += run == 10
    i, j = found or (0, 0)
    asleep = lambda t: i <= int((t - a) // 60) < j and st[int((t - a) // 60)] == "s"
    tp = fn = fp = tn = 0
    for t, v in ep:
        if v >= 0:
            p = asleep(t + 15)
            tp += v > 0 and p; fn += v > 0 and not p; fp += v == 0 and p; tn += v == 0 and not p
    inside = st[i:j]
    return {"onset": (a + i * 60 - on) / 60 if found else None, "wake": (a + j * 60 - off) / 60 if found else None,
            "tst": inside.count("s") - 30 * len(sl) / 60, "acc": (tp + tn) / (tp + tn + fp + fn),
            "sens": tp / max(1, tp + fn), "spec": tn / (tn + fp) if tn + fp >= 10 else None,
            "wakeups": sum(1 for x, _, ln in sleep._runs(inside) if x == "a" and ln >= sleep.WAKEUP),
            "lab_wakeups": lab_wakeups}


def loss(rows):
    """Mean of |onset| + |final wake| + |TST| error, minutes (no night found: 10 h each)."""
    return statistics.mean((abs(r["onset"]) if r["onset"] is not None else 600) +
                           (abs(r["wake"]) if r["wake"] is not None else 600) + abs(r["tst"]) for r in rows)


def summary(rows):
    mae = lambda k: statistics.mean(abs(r[k]) if r[k] is not None else 600 for r in rows)
    med = lambda k: statistics.median(r[k] if r[k] is not None else 600 for r in rows)
    mean = lambda k: statistics.mean(r[k] for r in rows if r[k] is not None)
    return (f"onset MAE {mae('onset'):.1f} (median {med('onset'):+.1f}) | final wake MAE {mae('wake'):.1f} "
            f"(median {med('wake'):+.1f}) | TST MAE {mae('tst'):.1f} (median {med('tst'):+.1f}) | "
            f"accuracy {mean('acc'):.3f} sensitivity {mean('sens'):.3f} specificity {mean('spec'):.3f} | "
            f"wake-ups MAE {statistics.mean(abs(r['wakeups'] - r['lab_wakeups']) for r in rows):.2f} "
            f"(bias {statistics.mean(r['wakeups'] - r['lab_wakeups'] for r in rows):+.2f})")


def show(name, rows):
    print(f"\n{name}")
    for sid, r in rows.items():
        f = lambda x: "  none" if x is None else f"{x:+6.0f}"
        print(f"  {sid:>8} onset {f(r['onset'])} wake {f(r['wake'])} TST {r['tst']:+6.0f} acc {r['acc']:.2f} "
              f"sens {r['sens']:.2f} spec {'  - ' if r['spec'] is None else format(r['spec'], '.2f')} "
              f"wake-ups {r['wakeups']} (lab {r['lab_wakeups']})")
    print("  ALL", summary(list(rows.values())), f"| loss {loss(list(rows.values())):.1f}")


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    sids = sorted(data)
    run = lambda c: with_constants(c, lambda: {sid: score(data[sid]) for sid in sids})
    show(f"sleep.py before (HR_ABOVE {BEFORE['HR_ABOVE']}, JOIN {BEFORE['JOIN']})", run(BEFORE))
    show(f"sleep.py now (HR_ABOVE {sleep.HR_ABOVE}, JOIN {sleep.JOIN})", run({}))
    grid = [(c, run(c)) for c in GRID]
    held, picks = {}, []
    for out in sids:
        c, res = min(grid, key=lambda g: loss([g[1][s] for s in sids if s != out]))
        held[out] = res[out]; picks.append(tuple(sorted(c.items())))
    print("\nleave-one-subject-out over HR_ABOVE x JOIN, held-out:")
    print("  ALL", summary(list(held.values())), f"| loss {loss(list(held.values())):.1f}")
    print("  settings picked:", {p: picks.count(p) for p in set(picks)})
