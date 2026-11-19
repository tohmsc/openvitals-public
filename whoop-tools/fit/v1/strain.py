# How scores.py MODELS["v1"] was fitted (2026-11-19). Needs numpy + scipy (not in requirements.txt):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   WHOOP_DB=/path/to/a/copy/of/whoop.db /tmp/fitenv/bin/python fit/v1/strain.py
import os
import sqlite3, json, numpy as np, math
from scipy.optimize import least_squares
c=sqlite3.connect(os.environ.get("WHOOP_DB", "whoop.db")); c.row_factory=sqlite3.Row
Z=["zone_zero_milli","zone_one_milli","zone_two_milli","zone_three_milli","zone_four_milli","zone_five_milli"]
T=lambda s: np.datetime64(s[:23]).astype('datetime64[ms]').astype(float)/1000
W=[dict(r) for r in c.execute("select * from api_workout where zone_raw like '%zone_zero%' and strain>0 and start>='2024-12' order by start")]
X=np.array([[json.loads(w["zone_raw"])[z]/6e4 for z in Z] for w in W]); S=np.array([w["strain"] for w in W])
real=~((X[:,1:].sum(1)<5)&(S>8)); X,S=X[real],S[real]
def kw(p): return np.concatenate([[math.exp(p[2])],[1.0],1+np.cumsum(np.exp(p[3:7]))])
def atan_map(p,L): return 21*0.5*(np.arctan(math.exp(p[0])*(np.log(np.maximum(L,1e-6))-p[1]))/(np.pi/2)+1)
def log_map(p,L): return p[0]*np.log1p(math.exp(p[1])*L)
p0=[0,4,-1,1,1,1,1]
n=len(S); cut=int(n*.7)
for name,f,pp in (("atan",atan_map,p0),("log",log_map,[4,-2,-1,1,1,1,1])):
    fit=lambda idx: least_squares(lambda p:f(p,X[idx]@kw(p))-S[idx],pp).x
    p=fit(np.arange(cut)); e=f(p,X[cut:]@kw(p))-S[cut:]
    pa=fit(np.arange(n)); ea=f(pa,X@kw(pa))-S
    print(name,"held-out MAE",np.abs(e).mean().round(3),"p90",np.percentile(np.abs(e),90).round(2),"| full in-sample",np.abs(ea).mean().round(3),"weights",kw(pa).round(3),"map",np.round(pa[:2],4))
    globals()[name]=pa
# days with atan
Wall=[(T(w["start"]),np.array([json.loads(w["zone_raw"])[z]/6e4 for z in Z])) for w in c.execute("select * from api_workout where zone_raw like '%zone_zero%'")]
C=[dict(r) for r in c.execute("select * from api_cycle where end is not null and strain>0 and start>='2024-12' order by start")]
R=[]
for cy in C:
    a,b=T(cy["start"]),T(cy["end"]); z=sum((x for t,x in Wall if a<=t<b),np.zeros(6)); R.append((cy["strain"],z,(b-a)/60-z.sum(),cy["avg_hr"]))
Sd=np.array([r[0] for r in R]); Zd=np.array([r[1] for r in R]); rest=np.array([r[2] for r in R]); avg=np.array([r[3] for r in R])
nd=len(Sd); cd=int(nd*.7)
for name,f in (("atan",atan_map),("log",log_map)):
    pa=globals()[name]; k=kw(pa)
    def md(q,i): L=Zd[i,1:]@k[1:]+(Zd[i,0]+rest[i]*np.clip(q[0]+q[1]*(avg[i]-70),0,1))*k[0]; return f(pa,L)
    q=least_squares(lambda q:md(q,np.arange(cd))-Sd[:cd],[0.6,0.02]).x; e=md(q,np.arange(cd,nd))-Sd[cd:]
    qa=least_squares(lambda q:md(q,np.arange(nd))-Sd,[0.6,0.02]).x
    print(name,"days held-out MAE",np.abs(e).mean().round(3),"p90",np.percentile(np.abs(e),90).round(2),"bias",e.mean().round(2),"phi full",qa.round(4))
    json.dump({"map":list(pa[:2]),"weights":list(kw(pa)),"phi":list(qa)},open(f"/tmp/strain_{name}.json","w"))
