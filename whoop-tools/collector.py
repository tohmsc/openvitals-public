#!/usr/bin/env python3
"""One-shot strap collector. Run by hand, from the dashboard's Resync, or with --loop.

Each run: if the strap is in range, connect, drain new stored data into ble_raw
(lossless), and stream `--live SECONDS` of realtime HR/RR into ble_hr. Also runs
the official API sync unless --no-api. Drains are incremental (the strap advances
its own read pointer as we ACK each batch).

If daemon.py (or live.py) is already holding the strap, this skips Bluetooth and
asks the daemon to drain right away instead, then does the rest (API, decode, backup).
"""
import asyncio, sys, time, os, signal, subprocess
import config, db, whoop
import whoop_api

HERE = config.HERE
LIVE_LOCK = os.path.join(HERE, ".live.lock")
STREAMERS = ("daemon.py", "live.py")

def env_addr():
    return config.env().get("WHOOP_BLE_ADDRESS") or None

def is_streamer(pid):
    """True if `pid` really is one of our strap-holding scripts.

    A lock that outlived its process (crash, power loss) can name a pid the system has since
    given to something unrelated. Without this check that stranger would keep the daemon from
    starting, and a Resync would send it SIGUSR1, which ends most programs.
    """
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return True                   # can't tell: assume the lock is right
    return any(s in cmd for s in STREAMERS)

def lock_holder():
    """PID of the process holding the strap (daemon.py / live.py), or None.

    A lock left behind by a process that died is ignored and removed, so a crash
    can't block Bluetooth collection forever.
    """
    try:
        with open(LIVE_LOCK) as f:
            pid = int(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        try:
            os.remove(LIVE_LOCK)
        except OSError:
            pass
        return None
    except PermissionError:
        pass                      # exists, owned by someone else
    if not is_streamer(pid):      # the pid was reused by another program: the lock is stale
        try:
            os.remove(LIVE_LOCK)
        except OSError:
            pass
        return None
    return pid

def holder_is_daemon():
    """True when the lock was written by daemon.py (which drains on SIGUSR1)."""
    try:
        with open(LIVE_LOCK) as f:
            return f.read().split()[1:] == ["daemon"]
    except OSError:
        return False

def raw_sink_for(c, skip=()):
    """Frame sink that stores every strap frame losslessly in ble_raw."""
    def raw_sink(ch, inner):
        if inner[0] in skip:
            return
        c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)",
                  (time.time(), ch[4:8], inner[0], inner.hex()))
        # raw_sink runs before whoop.py schedules the history ACK, so
        # committing here makes the batch durable before the strap trims it.
        if inner[0] == whoop.T_METADATA:
            c.commit()
    return raw_sink

def rr_ms(r):
    return [x for x in r["rr_ms"] if x]        # the strap sends whole milliseconds

# ---- the non-Bluetooth steps, shared with daemon.py ----
# Each returns False when it failed, so the daemon retries soon instead of a full interval later.
def run_api():
    if os.path.exists(os.path.join(HERE, ".tokens.json")):
        try:
            whoop_api.sync()
        except Exception as ex:
            print("API sync error:", ex)
            return False
    elif config.env().get("WHOOP_CLIENT_ID"):
        # set up for the API but never logged in here (or the login didn't move along with
        # an install): say so in the log, which is where someone looks when scores stop
        print("WHOOP API: not logged in here, so no sync. Run: python whoop_api.py login")
    return True

def run_decode(quiet=False):
    # decode captured raw frames into structured tables (battery SOC, device logs, samples)
    try:
        import decode; decode.run(quiet=quiet)
    except Exception as ex:
        print("decode error:", ex)
        return False
    return True

def run_sleep():
    # when you fell asleep and woke up (sleep.py), from the decoded movement and heart rate
    try:
        import sleep; sleep.update()
    except Exception as ex:
        print("sleep error:", ex)
        return False
    return True

def run_signals():
    # nightly skin temperature against your usual (signals.py), from strap_min and sleep_night
    try:
        import signals; signals.update()
    except Exception as ex:
        print("signals error:", ex)
        return False
    return True

def run_stress():
    # stress monitor (stress.py): today's 5-minute windows against your calm baseline
    try:
        import stress; stress.update()
    except Exception as ex:
        print("stress error:", ex)
        return False
    return True

def run_scores():
    # strap estimates of Strain, Sleep and Recovery for the last few days (scores.py)
    try:
        import scores; scores.update(days=3)
    except Exception as ex:
        print("scores error:", ex)
        return False
    return True

def run_backup(keep=30):
    # local snapshot (encrypted if .backup_key present)
    try:
        import backup; backup.snapshot(keep=keep)
    except Exception as ex:
        print("backup error:", ex)
        return False
    return True

def run_cloud():
    # cloud backup if a Cloudflare token is configured
    token = config.env().get("CF_API_TOKEN", "")
    if token and "your_cloudflare" not in token:
        try:
            import cloud_sync; cloud_sync.main()
        except Exception as ex:
            print("cloud sync error:", ex)
            return False
    return True

def run_archive():
    # move old raw frames to Cloudflare R2 and delete them locally (only if CF_R2_BUCKET is set)
    try:
        import archive
        if archive.configured():
            archive.run(quiet=True)
    except Exception as ex:
        print("archive error:", ex)
        return False
    return True

_tg_err = [None, 0.0]

def run_telegram():
    # Telegram: morning message, nudges, /status and /now (only if TELEGRAM_BOT_TOKEN is set)
    try:
        import telegram_report
        if telegram_report.configured():
            did = telegram_report.run_due()
            if did in ("paired", "sent", "nudged"):
                print("telegram:", {"paired": "linked to your chat", "sent": "morning message sent",
                                    "nudged": "nudge sent"}[did])
            _tg_err[0] = None
    except Exception as ex:
        # this runs twice a minute: say a given problem once an hour, not every time (e.g. offline)
        if str(ex) != _tg_err[0] or time.time() - _tg_err[1] > 3600:
            print("telegram error:", ex)
            _tg_err[:] = [str(ex), time.time()]
        return False
    return True

async def collect_once(live_secs=20, do_api=True):
    c = db.connect()
    holder = lock_holder()
    if holder and holder_is_daemon():
        print(f"strap is held by daemon.py (pid {holder}) — asking it to drain now")
        try:
            os.kill(holder, signal.SIGUSR1)
            await asyncio.sleep(5)
        except OSError:
            pass
        addr = None
    elif holder:
        print(f"live streamer (pid {holder}) holds the strap — skipping BLE this run")
        addr = None
    else:
        addr = env_addr() or await whoop.find()
    if addr:
        try:
            async with whoop.Whoop(addr) as w:
                print("strap connected:", addr)
                await w.init(); await asyncio.sleep(1.0)

                def rt(r):
                    if r["hr"] > 0:    # 0 = off the wrist: not a heart rate
                        c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                                  (time.time(), r["hr"], " ".join(map(str, rr_ms(r)))))
                w.raw_sink = raw_sink_for(c); w.on_realtime = rt

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
    elif not holder:
        print("strap not in range this run")

    if do_api:
        run_api()
    run_decode()
    run_backup()
    run_cloud()

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
