# How scores.py MODELS["v1"] was fitted (2026-11-19). Needs numpy + scipy (not in requirements.txt):
#   python3 -m venv /tmp/fitenv && /tmp/fitenv/bin/pip install numpy scipy
#   WHOOP_DB=/path/to/a/copy/of/whoop.db /tmp/fitenv/bin/python fit/v1/zones.py
import os
import sqlite3, json, collections, numpy as np
c=sqlite3.connect(os.environ.get("WHOOP_DB", "whoop.db")); c.row_factory=sqlite3.Row
Z=["zone_zero_milli","zone_one_milli","zone_two_milli","zone_three_milli","zone_four_milli","zone_five_milli"]
rhr=collections.defaultdict(list)
for r in c.execute("select created_at,resting_hr from api_recovery where resting_hr>0"): rhr[r[0][:7]].append(r[1])
rows=[(r["start"][:7],r["max_hr"],max(i for i in range(6) if json.loads(r["zone_raw"])[Z[i]]>0)) for r in c.execute("select * from api_workout where zone_raw like '%zone_zero%' and max_hr>0 order by start")]
def ok(M,rest,m,k,base):
    f=lambda p: rest+p*(M-rest) if base=="hrr" else p*M
    lo=f(0.4+0.1*k) if k else 0; hi=f(0.5+0.1*k) if k<5 else 1e9
    return lo-1<=m<hi+1
months=sorted(set(r[0] for r in rows))
tot={"hrr":0,"max":0}
for mth in months:
    rs=[r for r in rows if r[0]==mth]; prev=[x for k,v in rhr.items() if k<=mth for x in v][-30:]; rest=np.median(prev) if prev else 50
    out=[]
    for base in ("hrr","max"):
        s=max((sum(ok(M,rest,m,k,base) for _,m,k in rs)/len(rs),M) for M in range(175,216)); out.append((base,round(s[0],2),s[1])); tot[base]+=s[0]*len(rs)
    print(mth,len(rs),"rest",rest,out)
print({k:round(v/len(rows),3) for k,v in tot.items()})
# running max of workout max_hr
mx=0
for r in c.execute("select start,max_hr from api_workout where max_hr>0 order by start"):
    if r[1]>mx: mx=r[1]; print("new max",r[0][:10],mx)
