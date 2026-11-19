# How scores.py MODELS["v1"] was fitted (2026-11-19). Needs numpy + scipy (not in requirements.txt):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   WHOOP_DB=/path/to/a/copy/of/whoop.db /tmp/fitenv/bin/python fit/v1/sleep.py
import os
import sqlite3, numpy as np
from scipy.optimize import least_squares
c=sqlite3.connect(os.environ.get("WHOOP_DB", "whoop.db")); c.row_factory=sqlite3.Row
T=lambda s: np.datetime64(s[:23]).astype('datetime64[ms]').astype(float)/1000
S=[dict(r) for r in c.execute("select * from api_sleep where nap=0 and sleep_perf is not null and total_in_bed_ms>0 order by start")]
N=[(T(r["start"]),(r["total_in_bed_ms"]-r["total_awake_ms"])/36e5) for r in c.execute("select * from api_sleep where nap=1")]
C=[(T(r["start"]),T(r["end"]),r["strain"]) for r in c.execute("select * from api_cycle where end is not null and strain>0")]
f1=lambda s: 1.7/(1+np.exp((17-s)/3.5))
rows=[]
for i,s in enumerate(S):
    t=T(s["start"]); prev=[x for x in C if abs(x[1]-t)<1800]
    if not prev or i==0: continue
    nap=sum(h for nt,h in N if prev[0][0]<=nt<t)
    asleep=(s["total_in_bed_ms"]-s["total_awake_ms"])/36e5
    rows.append(dict(d=s["start"][:10],perf=s["sleep_perf"],asleep=asleep,strain=prev[0][2],nap=nap,eff=s["sleep_efficiency"],cons=s["sleep_consistency"] or np.nan,i=i))
print(len(rows))
# debt recursion with params: debt_i = r*max(0, need_{i-1}-asleep_{i-1})
def perf_hours(p,R):
    base,r=p[0],p[1]; out=[]; debt=0; prev=None
    for x in R:
        need=base+f1(x["strain"])+debt-x["nap"]
        out.append(min(100,100*x["asleep"]/need)); debt=r*max(0,need-x["asleep"])
    return np.array(out)
pre=[x for x in rows if x["d"]<"2025-05-01"]; post=[x for x in rows if x["d"]>="2025-05-08"]
y=np.array([x["perf"] for x in pre]); cut=int(len(pre)*.7)
p=least_squares(lambda p: (perf_hours(p,pre)-y)[:cut],[8,0.3]).x; e=(perf_hours(p,pre)-y)[cut:]
print("PRE-May hours/need: baseline %.2f h, debt carry %.2f"%tuple(p),"test MAE",np.abs(e).mean().round(2),"p90",np.percentile(np.abs(e),90).round(1),"n",len(e))
p_all=least_squares(lambda p: perf_hours(p,pre)-y,[8,0.3]).x; print("all-pre fit",p_all.round(3))
yp=np.array([x["perf"] for x in post]); hp=perf_hours(p_all,post)
print("POST-May with hours model: MAE",np.abs(hp-yp).mean().round(2),"n",len(post))
eff=np.array([x["eff"] for x in post]); cons=np.array([x["cons"] for x in post])
X=np.column_stack([hp,eff,cons,np.ones(len(post))]); ok=~np.isnan(cons)
cut=int(ok.sum()*.6); idx=np.where(ok)[0]
w=np.linalg.lstsq(X[idx[:cut]],yp[idx[:cut]],rcond=None)[0]; e=X[idx[cut:]]@w-yp[idx[cut:]]
print("POST composite w(hours,eff,cons,1)",w.round(3),"test MAE",np.abs(e).mean().round(2),"n test",len(e))
wa=np.linalg.lstsq(X[ok],yp[ok],rcond=None)[0]; print("post composite all-fit",wa.round(3),"in-sample MAE",np.abs(X[ok]@wa-yp[ok]).mean().round(2))
for x,h in list(zip(post,hp))[-8:]: print(x["d"],x["perf"],round(h,1),round(x["eff"],1),x["cons"])
X2=np.column_stack([hp,eff,np.ones(len(post))]); w2=np.linalg.lstsq(X2,yp,rcond=None)[0]
print("post no-cons all-fit",w2.round(3),"in-sample MAE",np.abs(X2@w2-yp).mean().round(2))
c2=int(len(post)*.6); w2t=np.linalg.lstsq(X2[:c2],yp[:c2],rcond=None)[0]; print("no-cons held-out MAE",np.abs(X2[c2:]@w2t-yp[c2:]).mean().round(2))
