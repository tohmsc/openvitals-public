#!/usr/bin/env python3
"""Localhost dashboard for your WHOOP data. Run: python dashboard.py -> http://localhost:8787"""
import json, time, os
from flask import Flask, jsonify, Response
import db

app = Flask(__name__)

def rows(c, q, *a):
    return [dict(r) for r in c.execute(q, a).fetchall()]

@app.route("/api/summary")
def summary():
    c = db.connect()
    return jsonify({
        "recovery": rows(c, "SELECT created_at,recovery_score,hrv_rmssd_ms,resting_hr,spo2,skin_temp_c "
                             "FROM api_recovery WHERE recovery_score IS NOT NULL ORDER BY created_at DESC LIMIT 60"),
        "sleep": rows(c, "SELECT start,end,sleep_perf,resp_rate,total_in_bed_ms,total_rem_ms,total_sws_ms,"
                         "total_light_ms,total_awake_ms FROM api_sleep ORDER BY start DESC LIMIT 60"),
        "cycle": rows(c, "SELECT start,strain,avg_hr,max_hr,kilojoule FROM api_cycle "
                         "WHERE strain IS NOT NULL ORDER BY start DESC LIMIT 60"),
        "workout": rows(c, "SELECT start,sport_name,strain,avg_hr,max_hr,distance_m FROM api_workout "
                           "ORDER BY start DESC LIMIT 30"),
        "live_hr": rows(c, "SELECT ts,hr,rr_ms FROM ble_hr ORDER BY ts DESC LIMIT 600"),
        "soc": rows(c, "SELECT rtc,soc_pct FROM device_soc ORDER BY rtc") if _has(c,"device_soc") else [],
        "logs": rows(c, "SELECT rtc,subsystem,message FROM device_logs ORDER BY rtc DESC LIMIT 40") if _has(c,"device_logs") else [],
        "counts": {t: c.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                   for t in ("api_recovery","api_sleep","api_cycle","api_workout","ble_hr","ble_raw")},
    })

def _has(c, t):
    return c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone() is not None

@app.route("/api/live")
def live():
    c = db.connect()
    r = c.execute("SELECT value FROM sync_state WHERE key='live_hr_now'").fetchone()
    cur = json.loads(r["value"]) if r else {}
    fresh = cur.get("hr") is not None and (time.time() - cur.get("ts", 0) < 8)
    recent = rows(c, "SELECT ts,hr FROM ble_hr ORDER BY ts DESC LIMIT 120")
    return jsonify({"live": fresh, "hr": cur.get("hr"), "rr_ms": cur.get("rr_ms", []),
                    "age": round(time.time() - cur.get("ts", 0), 1) if cur.get("ts") else None,
                    "recent": list(reversed(recent))})

PAGE = """<!doctype html><html><head><meta charset=utf-8><title>WHOOP local</title>
<script src="/static/chart.js"></script>
<style>body{font:14px -apple-system,sans-serif;margin:0;background:#0b0d10;color:#e7ecf0}
h1{font-size:18px;padding:16px 20px;margin:0;border-bottom:1px solid #1e2630}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:18px;padding:20px}
.card{background:#12161c;border:1px solid #1e2630;border-radius:12px;padding:14px}
.card h2{font-size:13px;margin:0 0 10px;color:#8aa0b3;text-transform:uppercase;letter-spacing:.05em}
.kpis{display:flex;gap:18px;flex-wrap:wrap;padding:0 20px 8px}
.kpi{background:#12161c;border:1px solid #1e2630;border-radius:12px;padding:12px 18px;min-width:120px}
.kpi .v{font-size:26px;font-weight:600}.kpi .l{font-size:11px;color:#8aa0b3}
.full{grid-column:1/3}</style></head><body>
<h1>WHOOP — local data <span id=counts style="color:#8aa0b3;font-weight:400"></span>
  <span id=livebadge style="float:right;font-size:14px;font-weight:600;color:#5f7488">○ offline</span></h1>
<div class=kpis id=kpis></div>
<div class=grid>
<div class=card><h2>Recovery %</h2><canvas id=rec></canvas></div>
<div class=card><h2>HRV (rmssd ms)</h2><canvas id=hrv></canvas></div>
<div class=card><h2>Day Strain</h2><canvas id=strain></canvas></div>
<div class=card><h2>Sleep stages (hrs)</h2><canvas id=sleep></canvas></div>
<div class=card><h2>Live heart rate (last session)</h2><canvas id=livehr></canvas></div>
<div class=card><h2>Strap battery (SOC %)</h2><canvas id=soc></canvas></div>
<div class=card full><h2>Firmware log (decoded from strap)</h2>
<div id=logs style="font:11px ui-monospace,Menlo,monospace;color:#9fb2c4;max-height:240px;overflow:auto;white-space:pre-wrap"></div></div>
</div>
<script>
const C=(id,cfg)=>new Chart(document.getElementById(id),cfg);
const line=(labels,data,color)=>({type:'line',data:{labels,datasets:[{data,borderColor:color,
 backgroundColor:color+'22',tension:.3,pointRadius:0,fill:true}]},
 options:{plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#5f7488',maxTicksLimit:6}},
 y:{ticks:{color:'#5f7488'},grid:{color:'#1e2630'}}}}});
fetch('/api/summary').then(r=>r.json()).then(d=>{
 const rev=a=>a.slice().reverse();
 document.getElementById('counts').textContent=
  ' — '+Object.entries(d.counts).map(([k,v])=>k.replace('api_','')+':'+v).join('  ');
 const last=d.recovery[0]||{}, lastSleep=d.sleep[0]||{}, lastCycle=d.cycle[0]||{};
 const kpi=(l,v)=>`<div class=kpi><div class=v>${v??'—'}</div><div class=l>${l}</div></div>`;
 document.getElementById('kpis').innerHTML=
  kpi('Recovery',last.recovery_score!=null?Math.round(last.recovery_score)+'%':'—')+
  kpi('HRV',last.hrv_rmssd_ms!=null?Math.round(last.hrv_rmssd_ms)+'ms':'—')+
  kpi('Resting HR',last.resting_hr!=null?Math.round(last.resting_hr):'—')+
  kpi('SpO2',last.spo2!=null?last.spo2.toFixed(1)+'%':'—')+
  kpi('Skin °C',last.skin_temp_c!=null?last.skin_temp_c.toFixed(1):'—')+
  kpi('Strain',lastCycle.strain!=null?lastCycle.strain.toFixed(1):'—')+
  kpi('Sleep perf',lastSleep.sleep_perf!=null?Math.round(lastSleep.sleep_perf)+'%':'—');
 const rc=rev(d.recovery);
 C('rec',line(rc.map(r=>r.created_at?.slice(5,10)),rc.map(r=>r.recovery_score),'#45d483'));
 C('hrv',line(rc.map(r=>r.created_at?.slice(5,10)),rc.map(r=>r.hrv_rmssd_ms),'#5aa9ff'));
 const cy=rev(d.cycle);
 C('strain',line(cy.map(r=>r.start?.slice(5,10)),cy.map(r=>r.strain),'#ff9f45'));
 const sl=rev(d.sleep).slice(-30);
 C('sleep',{type:'bar',data:{labels:sl.map(s=>s.start?.slice(5,10)),datasets:[
  {label:'REM',data:sl.map(s=>(s.total_rem_ms||0)/3.6e6),backgroundColor:'#7c5cff'},
  {label:'SWS',data:sl.map(s=>(s.total_sws_ms||0)/3.6e6),backgroundColor:'#3b6fe0'},
  {label:'Light',data:sl.map(s=>(s.total_light_ms||0)/3.6e6),backgroundColor:'#5aa9ff'}]},
  options:{plugins:{legend:{labels:{color:'#8aa0b3'}}},scales:{x:{stacked:true,ticks:{color:'#5f7488',maxTicksLimit:8}},
  y:{stacked:true,ticks:{color:'#5f7488'},grid:{color:'#1e2630'}}}}});
 const lh=rev(d.live_hr);
 window.lhChart=C('livehr',line(lh.map((_,i)=>i),lh.map(r=>r.hr),'#ff5a7a'));
 if(d.soc&&d.soc.length) C('soc',line(d.soc.map((_,i)=>i),d.soc.map(r=>r.soc_pct),'#45d483'));
 if(d.logs&&d.logs.length) document.getElementById('logs').textContent=
   d.logs.map(l=>`${l.rtc}  ${l.subsystem}: ${l.message}`).join('\\n');
});
function pollLive(){
 fetch('/api/live').then(r=>r.json()).then(L=>{
  const b=document.getElementById('livebadge');
  if(L.live){ b.textContent='● LIVE  '+L.hr+' bpm'; b.style.color='#45d483'; }
  else { b.textContent='○ offline'; b.style.color='#5f7488'; }
  if(window.lhChart && L.recent && L.recent.length){
   window.lhChart.data.labels=L.recent.map((_,i)=>i);
   window.lhChart.data.datasets[0].data=L.recent.map(r=>r.hr);
   window.lhChart.update('none');
  }
 }).catch(()=>{});
}
setInterval(pollLive,2000); pollLive();
</script></body></html>"""

@app.route("/static/chart.js")
def chartjs():
    with open(os.path.join(os.path.dirname(__file__), "static", "chart.umd.js")) as f:
        return Response(f.read(), mimetype="application/javascript")

@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    print("Dashboard: http://localhost:8787")
    app.run(host="127.0.0.1", port=8787, debug=False)
