# How scores.py MODELS["v1"] was fitted (2026-11-19). Needs numpy + scipy (not in requirements.txt):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   WHOOP_DB=/path/to/a/copy/of/whoop.db /tmp/fitenv/bin/python fit/v1/consistency.py
import os
import sqlite3, numpy as np
c=sqlite3.connect(os.environ.get("WHOOP_DB", "whoop.db")); c.row_factory=sqlite3.Row
T=lambda s: np.datetime64(s[:23]).astype('datetime64[ms]').astype(float)/1000
S=[(T(r["start"]),T(r["end"]),r["sleep_consistency"],r["start"][:10]) for r in c.execute("select * from api_sleep where nap=0 and sleep_consistency is not null order by start")]
allS=[(T(r["start"]),T(r["end"])) for r in c.execute("select * from api_sleep")]
def asleep_at(t): return any(a<=t<b for a,b in allS)
res=[]
for k in range(len(S)):
    end=S[k][1]
    for days,mode in ((4,"pairs"),(4,"all4")):
        # 1-min grid over each of the `days` 24h windows ending at this wake
        grid=np.array([[asleep_at(end-(d+1)*86400+m*60) for m in range(0,1440,5)] for d in range(days)])
        if mode=="pairs": v=np.mean([np.mean(grid[d]==grid[d+1]) for d in range(days-1)])
        else: v=np.mean(np.all(grid==grid[0],axis=0))
        res.append((mode,S[k][2],v*100))
for mode in ("pairs","all4"):
    r=np.array([(a,b) for m,a,b in res if m==mode]); 
    w=np.polyfit(r[:,1],r[:,0],1); e=np.polyval(w,r[:,1])-r[:,0]
    print(mode,"corr",np.corrcoef(r.T)[0,1].round(3),"raw MAE",np.abs(r[:,1]-r[:,0]).mean().round(2),"linear",w.round(3),"MAE after linear",np.abs(e).mean().round(2))
