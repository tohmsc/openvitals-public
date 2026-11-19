#!/usr/bin/env python3
"""Continuous LIVE streamer for the WHOOP strap.

Holds the BLE connection open, enables the 1 Hz realtime stream, and writes every
reading to the DB in real time (ble_hr + a `live_hr_now` marker the dashboard reads).
Auto-reconnects if the link drops. Creates .live.lock so the hourly collector skips
its own BLE connect while this is running (the strap bonds to one device at a time).

Run:  python live.py            # Ctrl-C to stop
Wear the strap for a real pulse; on the charger it reports a flat resting value.
"""
import asyncio, os, time, json, signal
import db, whoop, collector

LOCK = collector.LIVE_LOCK
_env_addr = collector.env_addr
_stop = False

def _cleanup(*a):
    global _stop; _stop = True

async def stream_once(c):
    addr = _env_addr() or await whoop.find()
    if not addr:
        print("strap not found"); return
    async with whoop.Whoop(addr) as w:
        print("LIVE connected:", addr)
        await w.init(); await asyncio.sleep(0.8)

        def rt(r):
            now = time.time()
            rr = [x for x in r["rr_ms"] if x]
            if r["hr"] > 0:        # 0 = off the wrist: not a heart rate
                c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                          (now, r["hr"], " ".join(map(str, rr))))
            db.set_state(c, "live_hr_now", json.dumps({"hr": r["hr"], "rr_ms": rr,
                                                       "ts": now, "rtc": r["ts"]}))
            c.commit()
            print(f"\rLIVE  {r['hr']:3d} bpm   RR {rr}      ", end="", flush=True)
        w.on_realtime = rt
        await w.hr_broadcast(True)
        await w.realtime(True)
        while not _stop and w.client.is_connected:
            await asyncio.sleep(1)
        try:
            await w.realtime(False)
        except Exception:
            pass

async def main():
    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)
    holder = collector.lock_holder()
    if holder:
        print(f"the strap is already held by pid {holder} (daemon.py streams live on its own) — not starting")
        return
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    c = db.connect()
    try:
        while not _stop:
            try:
                await stream_once(c)
            except Exception as ex:
                print("\nlink error, reconnecting in 5s:", ex)
            if not _stop:
                db.set_state(c, "live_hr_now", json.dumps({"hr": None, "ts": time.time()}))
                await asyncio.sleep(5)
    finally:
        if os.path.exists(LOCK):
            os.remove(LOCK)
        print("\nLIVE stopped.")

if __name__ == "__main__":
    asyncio.run(main())
