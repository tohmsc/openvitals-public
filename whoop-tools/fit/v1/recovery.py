# How scores.py MODELS["v1"] was fitted (2026-11-19). Needs numpy + scipy (not in requirements.txt):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   WHOOP_DB=/path/to/a/copy/of/whoop.db /tmp/fitenv/bin/python fit/v1/recovery.py
import os
import sqlite3, numpy as np, math
from scipy.optimize import least_squares
c=sqlite3.connect(os.environ.get("WHOOP_DB", "whoop.db")); c.row_factory=sqlite3.Row
R=[dict(r) for r in c.execute("select r.created_at,r.recovery_score y,r.resting_hr rhr,r.hrv_rmssd_ms hrv,s.sleep_perf sp,s.resp_rate rr from api_recovery r left join api_sleep s on s.id=r.sleep_id where r.recovery_score is not null and r.resting_hr>0 and r.hrv_rmssd_ms>0 order by r.created_at")]
print(len(R), "with sleep perf", sum(1 for r in R if r["sp"] is not None))
T=lambda s: np.datetime64(s[:19]).astype(float)
med=np.median
def mad(x,fl,de,ce):
    if len(x)<5: return de
    m=med(x); return min(ce,max(fl,1.4826*med(np.abs(np.array(x)-m))))
F=[]
for i,x in enumerate(R):
    t=T(x["created_at"]); prev=[p for p in R[:i] if t-T(p["created_at"])<=30*86400]
    if len(prev)<10 or x["sp"] is None or x["rr"] is None: continue
    rh=[p["rhr"] for p in prev]; hv=[math.log(p["hrv"]) for p in prev]; rr=[p["rr"] for p in prev if p["rr"]]
    F.append((x["y"],(x["rhr"]-med(rh))/mad(rh,2,3,6),(math.log(x["hrv"])-med(hv))/mad(hv,.1,.15,.35),(x["sp"]-80)/10,(x["rr"]-med(rr))/0.5 if rr else 0))
F=np.array(F); y=F[:,0]; n=len(y); cut=int(n*.7)
zone=lambda v: np.where(v>=67,2,np.where(v<34,0,1))
def ev(name,cols,logistic=False):
    X=np.column_stack([np.ones(n)]+[F[:,c] for c in cols])
    if logistic:
        f=lambda p,X: 100/(1+np.exp(-(X@p)))
        p=least_squares(lambda p: f(p,X[:cut])-y[:cut],np.zeros(X.shape[1])).x; pr=f(p,X)
    else:
        p=np.linalg.lstsq(X[:cut],y[:cut],rcond=None)[0]; pr=np.clip(X@p,1,99)
    e=pr[cut:]-y[cut:]; print(f"{name:32s} test MAE {np.abs(e).mean():.2f} zone match {np.mean(zone(pr[cut:])==zone(y[cut:])):.2f} p={p.round(3)}")
ev("linear rhr+hrv (current v0)",[1,2]); ev("logistic rhr+hrv",[1,2],True)
ev("linear rhr+hrv+sleep",[1,2,3]); ev("logistic rhr+hrv+sleep",[1,2,3],True)
ev("logistic rhr+hrv+sleep+resp",[1,2,3,4],True)
X=np.column_stack([np.ones(n),F[:,1],F[:,2],F[:,3]]); print("FULL linear rhr+hrv+sleep",np.linalg.lstsq(X,y,rcond=None)[0].round(3))
X=np.column_stack([np.ones(n),F[:,1],F[:,2]]); print("FULL linear rhr+hrv",np.linalg.lstsq(X,y,rcond=None)[0].round(3))
