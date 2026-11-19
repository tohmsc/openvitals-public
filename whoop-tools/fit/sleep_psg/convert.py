# Turn the public sleep-lab dataset into the per-minute inputs sleep.py reads (movement, heart rate),
# plus the lab's sleep labels. Needs numpy (not in requirements.txt). Rerun:
#   mkdir /tmp/sleep-accel && cd /tmp/sleep-accel && curl -LO https://physionet.org/static/published-projects/sleep-accel/motion-and-heart-rate-from-a-wrist-worn-wearable-and-labeled-sleep-from-polysomnography-1.0.0.zip && unzip -q *.zip
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy
#   /tmp/fitenv/bin/python -I fit/sleep_psg/convert.py /tmp/sleep-accel/motion-and-heart-rate-*-1.0.0 /tmp/psg.json
#   python3 -I fit/sleep_psg/validate.py /tmp/psg.json            (run from whoop-tools/)
#
# Dataset: Walch et al. 2019, "Motion and heart rate from a wrist-worn wearable and labeled sleep from
# polysomnography", PhysioNet sleep-accel 1.0.0 (31 adults, one lab night each, Apple Watch).
# Times in the files are seconds from the start of the lab recording.
#   movement   Apple Watch acceleration (~50 Hz, in g) is cut to 1 Hz by keeping the first sample of each
#              second (an instantaneous 1 Hz reading, like the strap's history). Then exactly as
#              decode.add_motion: |change of (x, y, z)| between records at most 2 s apart, summed per minute,
#              divided by the number of steps; minutes with fewer than 20 steps are missing (as sleep.minutes).
#              --mean averages each second's samples instead (a smoother 1 Hz; the strap's own filtering is
#              unknown, so validate.py is run on both).
#   heart rate The watch reads heart rate about every 5 s asleep (the strap: every second), so a minute has
#              ~12 readings, not 60. Each minute is the median of whatever readings it has; sleep.minutes'
#              "at least 10 readings" rule is not applied (it is about strap coverage, not accuracy).
#   labels     30 s epochs: -1 unscored, 0 wake, 1-4 NREM, 5 REM.
# The window given to the detector is 90 min before the recording to 90 min after it (the watch has some
# data there for most people; minutes without data are missing, as when the strap is not read).
import sys, os, glob, json
import numpy as np

d, out = sys.argv[1], sys.argv[2]
mean = "--mean" in sys.argv
PRE = POST = 90

res = {}
for f in sorted(glob.glob(os.path.join(d, "labels", "*_labeled_sleep.txt"))):
    sid = os.path.basename(f).split("_")[0]
    lab = np.loadtxt(f)
    a = -PRE * 60
    n = int((lab[-1, 0] + 30 + POST * 60 - a) // 60)
    acc = np.loadtxt(os.path.join(d, "motion", sid + "_acceleration.txt"))
    acc = acc[(acc[:, 0] >= a - 5) & (acc[:, 0] < a + n * 60 + 5)]
    acc = acc[np.argsort(acc[:, 0], kind="stable")]
    sec = np.floor(acc[:, 0]).astype(np.int64)
    if mean:
        u, idx, cnt = np.unique(sec, return_index=True, return_counts=True)
        g = np.add.reduceat(acc[:, 1:4], idx, axis=0) / cnt[:, None]
    else:
        u, idx = np.unique(sec, return_index=True)
        g = acc[idx, 1:4]
    move_sum, steps = np.zeros(n), np.zeros(n, dtype=int)
    ok = np.diff(u) <= 2
    mi = ((u[1:] - a) // 60).astype(int)
    sel = ok & (mi >= 0) & (mi < n)
    np.add.at(move_sum, mi[sel], np.linalg.norm(np.diff(g, axis=0), axis=1)[sel])
    np.add.at(steps, mi[sel], 1)
    move = [float(move_sum[i] / steps[i]) if steps[i] >= 20 else None for i in range(n)]
    hr = np.loadtxt(os.path.join(d, "heart_rate", sid + "_heartrate.txt"), delimiter=",")
    hr = hr[(hr[:, 0] >= a) & (hr[:, 0] < a + n * 60) & (hr[:, 1] > 0)]
    hmi = ((hr[:, 0] - a) // 60).astype(int)
    hrm = [float(np.median(hr[hmi == i, 1])) if (hmi == i).any() else None for i in range(n)]
    res[sid] = {"a": a, "move": move, "hr": hrm, "epochs": [[float(t), int(v)] for t, v in lab]}
json.dump(res, open(out, "w"))
print(f"{len(res)} subjects -> {out}")
