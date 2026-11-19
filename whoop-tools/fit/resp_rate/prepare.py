# Turns PhysioNet recordings (ECG beat annotations + a respiration channel) into what our strap
# would have recorded, plus a reference breathing rate per minute. Output: one .json per record.
#
# Datasets (open, PhysioNet; download each into its own empty directory outside the repo):
#   Fantasia  https://physionet.org/content/fantasia/1.0.0/   40 healthy adults, 2 h supine at rest
#   SLPDB     https://physionet.org/content/slpdb/1.0.0/      MIT-BIH polysomnography, 18 nights
#   mkdir -p /tmp/resp-data/fantasia /tmp/resp-data/slpdb
#   (fetch <record>.hea .dat .ecg, and .st for slpdb, for every name in each RECORDS file)
#
# Rerun (numpy + scipy + wfdb in a scratch venv; -I so nothing in the data dirs gets imported):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy wfdb
#   /tmp/fitenv/bin/python -I fit/resp_rate/prepare.py /tmp/resp-data /tmp/resp-prep
#   /tmp/fitenv/bin/python -I fit/resp_rate/fit.py /tmp/resp-prep
#
# Strap degradation (WHOOP 4.0 is optical, it reports intervals once a second):
#   beat times jittered by 10 ms (pulse-wave timing noise), 3% of beats missed (two intervals
#   merge into one, which parse_rr then rejects), intervals quantised to 1/1024 s, grouped
#   into one row per second with a 5-beat heart rate, like ble_hr.
# Reference: the respiration channel, band-passed 0.08-0.8 Hz, per 60/90/120 s window starting
# every 60 s: breaths counted from upward crossings, kept only when the count agrees with
# the spectral peak within 1.5 breaths/min (otherwise the breathing itself was irregular, e.g.
# apnoea, and there is no single rate to compare with).
import json, os, sys
import numpy as np, wfdb
from scipy.signal import butter, sosfiltfilt, resample_poly

SEED = 7
BEATS = set("NLRBAaJSVrFejnE/fQ?")


def resp_channel(names):
    for pref in ("RESP", "Resp (chest)", "Resp (abdomen)", "Resp (sum)", "Resp (nasal)"):
        for i, n in enumerate(names):
            if n.strip().lower() == pref.lower():
                return i
    for i, n in enumerate(names):
        if "resp" in n.lower():
            return i
    return None


def ref_rates(sig, fs, t_end, win=120, step=60):
    """{window start (s): breaths/min} where counting and spectrum agree."""
    sos = butter(2, [0.08, 0.8], btype="band", fs=10.0, output="sos")
    x = resample_poly(np.nan_to_num(sig), 10, int(round(fs))) if fs != 10 else sig
    x = sosfiltfilt(sos, x)
    out = {}
    for a in range(0, int(t_end) - win + 1, step):
        seg = x[a * 10:(a + win) * 10]
        if len(seg) < win * 10 or not np.std(seg) > 0:
            continue
        seg = seg - seg.mean()
        h = 0.3 * np.std(seg)                      # hysteresis: a crossing must clear +-h
        state, ups = 0, []
        for k, v in enumerate(seg):
            if state <= 0 and v > h:
                if state < 0:
                    ups.append(k)
                state = 1
            elif state >= 0 and v < -h:
                state = -1
        if len(ups) < 3:
            continue
        cnt = 60.0 * (len(ups) - 1) / ((ups[-1] - ups[0]) / 10.0)
        f = np.fft.rfftfreq(8192, 0.1)
        P = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), 8192)) ** 2
        band = (f >= 0.08) & (f <= 0.8)
        spec = 60.0 * f[band][np.argmax(P[band])]
        if abs(cnt - spec) <= 1.5:
            out[a] = round(cnt, 3)
    return out


def strap_rows(bt, rng):
    """ble_hr-like rows [(ts, hr, rr_ms)] from true beat times (s)."""
    bt = np.sort(bt + rng.normal(0, 0.010, len(bt)))
    keep = rng.random(len(bt)) >= 0.03               # missed beats
    keep[0] = True
    bt = bt[keep]
    rr = np.diff(bt) * 1000.0
    t = bt[1:]
    rr = np.round(rr * 1.024) / 1.024                 # 1/1024 s units
    rows, j = [], 0
    hist = []
    for s in range(int(np.ceil(t[0])), int(t[-1]) + 1):
        xs = []
        while j < len(t) and t[j] <= s:
            if 280 < rr[j] < 2000:
                xs.append(rr[j]); hist.append(rr[j])
            j += 1
        hist = hist[-5:]
        if not hist:
            continue
        hr = int(round(60000.0 / np.mean(hist)))
        rows.append((float(s), hr, " ".join(f"{x:.1f}" for x in xs)))
    return rows


def main(src, dst):
    os.makedirs(dst, exist_ok=True)
    rng = np.random.default_rng(SEED)
    for ds in ("fantasia", "slpdb"):
        d = os.path.join(src, ds)
        for rec in sorted(f[:-4] for f in os.listdir(d) if f.endswith(".hea")):
            p = os.path.join(d, rec)
            try:
                h = wfdb.rdheader(p)
                ri = resp_channel(h.sig_name)
                if ri is None:
                    print(rec, "no respiration channel"); continue
                ann = wfdb.rdann(p, "ecg")
                sig = wfdb.rdrecord(p, channels=[ri]).p_signal[:, 0]
            except Exception as e:                       # incomplete download etc.
                print(rec, "skipped:", e); continue
            fs = h.fs
            bt = np.array([s / fs for s, sym in zip(ann.sample, ann.symbol) if sym in BEATS])
            sleep = None
            if ds == "slpdb" and os.path.exists(p + ".st"):
                st = wfdb.rdann(p, "st")                # 30 s epochs: W, 1-4, R, ...
                sleep = [[float(s / fs), (a or " ")[0] in "1234R"] for s, a in zip(st.sample, st.aux_note)]
            out = {"record": rec, "dataset": ds, "subject": rec[:5] if ds == "slpdb" else rec,
                   "resp_channel": h.sig_name[ri], "rows": strap_rows(bt, rng),
                   "ref": {w: ref_rates(sig, fs, len(sig) / fs, win=w) for w in (60, 90, 120)},
                   "sleep_epochs": sleep}
            with open(os.path.join(dst, rec + ".json"), "w") as f:
                json.dump(out, f)
            print(rec, h.sig_name[ri], len(bt), "beats", len(out["ref"][120]), "reference 2-min windows")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
