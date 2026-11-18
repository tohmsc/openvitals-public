#!/usr/bin/env python3
"""Background strap collector. Run periodically (launchd) or with --loop.

Each run: if the strap is in range, connect, drain new stored data into ble_raw
(lossless), and stream `--live SECONDS` of realtime HR/RR into ble_hr. Also runs
the official API sync unless --no-api. Safe to run hourly; drains are incremental
(the strap advances its own read pointer as we ACK each batch).
"""
import asyncio, sys, time, os
import db, whoop
import whoop_api

def env_addr():
    p = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(p):
        for line in open(p):
            if line.startswith("WHOOP_BLE_ADDRESS="):
                return line.strip().split("=", 1)[1]
    return None

async def collect_once(live_secs=20, do_api=True):
    c = db.connect()
    live_lock = os.path.join(os.path.dirname(__file__), ".live.lock")
    addr = env_addr() or await whoop.find()
    if os.path.exists(live_lock):
        print("live streamer holds the strap — skipping BLE this run")
        addr = None
    if addr:
        try:
            async with whoop.Whoop(addr) as w:
                print("strap connected:", addr)
                await w.init(); await asyncio.sleep(1.0)

                def raw_sink(ch, inner):
                    c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)",
                              (time.time(), ch[4:8], inner[0], inner.hex()))
                    # raw_sink runs before whoop.py schedules the history ACK, so
                    # committing here makes the batch durable before the strap trims it.
                    if inner[0] == whoop.T_METADATA:
                        c.commit()
                def rt(r):
                    rr = [round(x/1024*1000,1) for x in r["rr_1024"] if x]
                    c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                              (time.time(), r["hr"], " ".join(map(str, rr))))
                w.raw_sink = raw_sink; w.on_realtime = rt

                n0 = c.execute("SELECT COUNT(*) n FROM ble_raw").fetchone()["n"]
                await w.drain(timeout=90); c.commit()
                n1 = c.execute("SELECT COUNT(*) n FROM ble_raw").fetchone()["n"]
                print(f"drained {n1-n0} new raw frames")

                if live_secs > 0:
                    await w.realtime(True)
                    await asyncio.sleep(live_secs)
                    await w.realtime(False)
                    c.commit()
                db.set_state(c, "last_ble_sync", time.time())
        except Exception as ex:
            print("strap not reachable this run:", ex)
    else:
        print("strap not in range this run")

    if do_api and os.path.exists(os.path.join(os.path.dirname(__file__), ".tokens.json")):
        try:
            whoop_api.sync()
        except Exception as ex:
            print("API sync error:", ex)

    # decode captured raw frames into structured tables (realtime HR, battery SOC, device logs)
    try:
        import decode; decode.run()
    except Exception as ex:
        print("decode error:", ex)

    # local backup every run (encrypted if .backup_key present)
    try:
        import backup; backup.snapshot()
    except Exception as ex:
        print("backup error:", ex)

    # cloud backup if a Cloudflare token is configured
    envp = os.path.join(os.path.dirname(__file__), ".env")
    has_cf = any(l.startswith("CF_API_TOKEN=") and "your_cloudflare" not in l
                 for l in open(envp)) if os.path.exists(envp) else False
    if has_cf:
        try:
            import cloud_sync; cloud_sync.main()
        except Exception as ex:
            print("cloud sync error:", ex)

async def main():
    live = 20; loop = False; interval = 3600; do_api = True
    args = sys.argv[1:]
    if "--no-api" in args: do_api = False
    if "--loop" in args: loop = True
    if "--live" in args: live = int(args[args.index("--live")+1])
    if "--interval" in args: interval = int(args[args.index("--interval")+1])
    while True:
        print("=== collect", time.strftime("%Y-%m-%d %H:%M:%S"), "===")
        await collect_once(live, do_api)
        if not loop: break
        await asyncio.sleep(interval)

if __name__ == "__main__":
    asyncio.run(main())
