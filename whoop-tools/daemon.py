#!/usr/bin/env python3
"""Always-on strap daemon: the laptop is the strap's host whenever it is nearby.

One long-running process replaces the hourly collector and live.py:

  strap in range  -> connect, drain everything it stored while away (lossless, into
                     ble_raw), then hold the link: 1 Hz live HR/RR into ble_hr and
                     another drain every --drain-every seconds
  link drops      -> (walked away, laptop slept, strap rebooted) keep looking and
                     reconnect; the strap keeps recording on its own in the meantime
  Mac wakes       -> drop the old link at once and connect fresh; if the strap still
                     can't be reached a couple of minutes later, restart the process
                     (macOS Bluetooth can stay broken inside a process after a sleep)
  anything hangs  -> a watchdog thread restarts the process (see Daemon._restart)
  on timers       -> WHOOP API sync (hourly), decode (5 min), backup (daily),
                     cloud push (hourly), raw-frame archive to R2 + local cleanup (hourly),
                     daily health summary to Telegram
                     — in a worker thread, with or without the strap

It holds .live.lock so nothing else fights for the strap (one host at a time).
`collector.py` and the dashboard's Resync notice the lock and send SIGUSR1, which
makes the daemon drain immediately.

Run:  python daemon.py                 # Ctrl-C to stop
      python daemon.py --no-drain      # live only; leave stored history on the strap
      python daemon.py --no-cloud      # a separate cloud_sync job handles the cloud push
Keep it running with launchd: see com.whoop.daemon.plist.
"""
import argparse, asyncio, contextlib, json, os, signal, sys, threading, time
import db, whoop, collector, alarm

LOCK = collector.LIVE_LOCK


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


async def find_strap(timeout=15.0):
    """Quiet scan for a WHOOP by its service UUID. Returns an address or None."""
    from bleak import BleakScanner
    d = await BleakScanner.find_device_by_filter(
        lambda dev, adv: whoop.WHOOP_SVC in (adv.service_uuids or []), timeout=timeout)
    return d.address if d else None


class Daemon:
    def __init__(self, drain_every=300, do_drain=True, do_api=True, do_cloud=True,
                 strap=whoop.Whoop, find=find_strap, addr=collector.env_addr):
        self.drain_every = drain_every
        self.do_drain = do_drain
        self.strap, self.find, self.addr = strap, find, addr
        # seconds between housekeeping runs; last-run times persist in sync_state
        self.jobs = [("decode", 300, lambda: collector.run_decode(quiet=True)),
                     ("sleep", 300, collector.run_sleep),                                # after decode
                     ("signals", 600, collector.run_signals),                   # after sleep: skin temp per night
                     ("stress", 300, collector.run_stress),                     # after sleep: skips sleep
                     ("scores", 600, collector.run_scores),                     # after decode: uses its heart rate
                     ("backup", 86400, lambda: collector.run_backup(keep=7))]   # a week of dailies
        if do_cloud:
            self.jobs.append(("cloud", 3600, collector.run_cloud))
        # last: ship old raw frames to R2 and delete them locally (no-op unless CF_R2_BUCKET is set)
        self.jobs.append(("archive", 3600, collector.run_archive))
        # morning message, nudges and /status replies (no-op unless TELEGRAM_BOT_TOKEN is set);
        # runs on every housekeeping pass so a command gets its answer within about 30 s
        self.jobs.append(("telegram", 20, collector.run_telegram))
        if do_api:
            self.jobs.insert(0, ("api", 3600, collector.run_api))
        self.settle = 1.0          # pause after the init handshake
        self.drain_timeout = 90
        self.drain_stall = 20      # a drain with no frame at all for this long is abandoned
        self.last_frame = 0.0      # when the strap last sent anything
        self.stall = 60            # no live frame for this long -> reconnect
        self.retry_min, self.retry_max = 5, 30
        self.retry_job = 300       # a failed housekeeping job runs again after this long
        self.tick = 1.0
        # CoreBluetooth calls (scan, connect, notify setup, writes, disconnect) can wait
        # forever after the Mac sleeps. Each gets a time limit; past it the attempt is
        # dropped and the main loop tries again.
        self.find_timeout = 60
        self.link_timeout = 60
        # Staying reconnectable. time.monotonic() stops while the Mac sleeps and
        # time.time() doesn't, so a jump in their difference means the Mac was asleep.
        self.slept_min = 30        # a clock jump this big (s) counts as a sleep
        self.wake_grace = 120      # after a wake the strap must be linked within this long,
        self.idle_restart = 1800   # otherwise within this long of the last link
        self.watchdog = 300        # main loop silent this long = stuck: restart
        self.heartbeat = time.monotonic()
        self.reconnect_by = self.heartbeat + self.idle_restart
        self._skew = time.time() - time.monotonic()
        self.restart = self._restart
        self.c = None
        self.stop = asyncio.Event()
        self.drain_now = asyncio.Event()
        self.frames = 0            # stored (non-live) frames this process captured
        self.last_rt = 0.0
        self.offset = None         # wall - strap RTC, as last recorded
        self._said = None
        self.seg = None            # host_log segment being written: [state, start, last seen, last written]
        self.alarm_retry = 60      # a strap alarm that did not take is tried again after this long
        self._alarm_next = 0.0

    # ---------------- staying reconnectable ----------------
    def beat(self):
        self.heartbeat = time.monotonic()

    def slept(self):
        """Seconds the Mac was asleep since the last call (0 if it wasn't)."""
        skew = time.time() - time.monotonic()
        gap, self._skew = skew - self._skew, skew
        if gap > self.slept_min:
            self._close_seg()
            now = time.time()
            self._log_seg("asleep", now - gap, now)
            return gap
        return 0

    # ---------------- what the laptop was doing (host_log) ----------------
    def _log_seg(self, state, start, end):
        if self.c is None:
            return
        try:
            self.c.execute("INSERT OR REPLACE INTO host_log(start,end,state) VALUES(?,?,?)", (start, end, state))
            self.c.commit()
        except Exception as ex:
            log("host log write error:", ex)

    def _close_seg(self):
        if self.seg:
            self._log_seg(self.seg[0], self.seg[1], self.seg[2])
            self.seg = None

    def track(self, state):
        """Note that the laptop is in `state` ('linked' or 'searching') right now. Written about
        once a minute; a pause of more than 90 s (asleep, or the process stuck) ends the segment,
        so a hole in host_log means nobody was watching."""
        now = time.time()
        s = self.seg
        if s and s[0] == state and now - s[2] < 90:
            s[2] = now
            if now - s[3] >= 60:
                s[3] = now
                self._log_seg(state, s[1], now)
            return
        self._close_seg()
        self.seg = [state, now, now, now]
        self._log_seg(state, now, now)

    def _restart(self, why):
        """Replace this process with a fresh copy of itself (same pid, so the lock stays ours).

        A new process gets a new CoreBluetooth manager. That is what cured the stuck
        state seen after an overnight sleep (2026-11-19): the old process sat idle for
        8 hours with the strap advertising next to it; a restart connected in 3 s.
        """
        log(f"{why} — restarting the daemon")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except OSError as ex:
            log("restart failed:", ex)
            os._exit(75)               # launchd's KeepAlive starts a new one

    def _watch(self):
        """Watchdog thread: if the main loop stops beating, restart the process.

        It runs outside the event loop, so it still works when the loop is stuck in a
        call that never returns. Monotonic time pauses while the Mac sleeps, so a long
        sleep doesn't trip it.
        """
        while not self.stop.is_set():
            time.sleep(min(10, self.watchdog / 4))
            quiet = time.monotonic() - self.heartbeat
            if quiet > self.watchdog and not self.stop.is_set():
                self.restart(f"main loop stuck for {int(quiet)}s")
                return

    def say(self, msg):
        """Log a status line once, not on every retry."""
        if msg != self._said:
            self._said = msg
            log(msg)

    # ---------------- live stream ----------------
    def on_realtime(self, r):
        try:
            now = time.time()
            self.last_rt = self.last_frame = time.monotonic()
            rr = collector.rr_ms(r)
            c = self.c
            if r["hr"] > 0:            # 0 = off the wrist; don't store a day of zeros
                c.execute("INSERT OR REPLACE INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)",
                          (now, r["hr"], " ".join(map(str, rr))))
            c.execute("INSERT INTO sync_state(key,value) VALUES('live_hr_now',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (json.dumps({"hr": r["hr"], "rr_ms": rr, "ts": now, "rtc": r["ts"]}),))
            # Remember how the strap's clock maps to real time (once per connect, and
            # again if it jumps), so stored records can be placed in time later.
            offset = now - r["ts"]
            if self.offset is None or abs(offset - self.offset) > 5:
                self.offset = offset
                c.execute("INSERT OR REPLACE INTO clock_sync(ts,rtc,offset) VALUES(?,?,?)",
                          (now, r["ts"], round(offset, 3)))
            c.commit()
        except Exception as ex:        # never let a DB hiccup kill the BLE callback
            log("live write error:", ex)

    def on_battery(self, pct, key="battery_now"):
        """The strap's answer to GetBatteryLevel: the one strap battery reading that is current.
        With key="pack_battery_now": the Battery Level characteristic (likely the battery pack)."""
        try:
            db.set_state(self.c, key, json.dumps({"soc": pct, "ts": time.time(), "src": "strap"}))
        except Exception as ex:
            log("battery write error:", ex)

    async def read_batteries(self, w):
        await self._ble(w.battery())           # the strap's answer arrives via on_battery
        try:
            pack = await self._ble(w.pack_battery())
        except Exception as ex:                # optional: never drop the link over it
            log("battery pack read failed:", ex)
            return
        if pack is not None:
            self.on_battery(pack, "pack_battery_now")

    def mark_offline(self):
        try:
            db.set_state(self.c, "live_hr_now", json.dumps({"hr": None, "ts": time.time()}))
        except Exception as ex:
            log("state write error:", ex)

    # ---------------- buzz / alarm requests (alarm.py) ----------------
    def on_event(self, eid, inner):
        if eid in whoop.EVENT_NAMES:
            try:
                alarm.note_event(self.c, eid)
                log(f"strap: {whoop.EVENT_NAMES[eid]}")
            except Exception as ex:
                log("event write error:", ex)

    async def strap_jobs(self, w):
        """Run queued buzz requests and keep the strap's alarm in step. Called every tick.
        A Bluetooth time-out ends the session (reconnect); any other error is only logged,
        so a bad request can never stop the live stream."""
        try:
            await self._strap_jobs(w)
        except (asyncio.TimeoutError, TimeoutError):
            raise
        except Exception as ex:
            log("strap request error:", ex)
            self._alarm_next = time.time() + self.alarm_retry

    async def _strap_jobs(self, w):
        poke = await alarm.run_requests(w, self.c)
        now = time.time()
        if alarm.ring_due(self.c, now):            # strap didn't confirm its alarm: ring it ourselves
            alarm.mark_rang(self.c, now)
            r = await self._ble(w.run_alarm())
            log("alarm time: rang the strap from the daemon" + ("" if r else " (no answer)"))
        if (poke or now >= self._alarm_next) and alarm.due_for_sync(self.c, now):
            msg = await self._ble(alarm.sync(w, self.c, now))
            if msg:
                log(msg)
            ok = alarm.armed(self.c).get("ok") or alarm.desired(alarm.get(self.c), now) is None
            self._alarm_next = now + (0 if ok else self.alarm_retry)

    # ---------------- one connection ----------------
    async def _or_stop(self, coro):
        """Await coro, but give up as soon as the daemon is asked to stop."""
        task = asyncio.ensure_future(coro)
        stopper = asyncio.ensure_future(self.stop.wait())
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if not task.done():
            task.cancel()
            return None
        return task.result()

    async def drain(self, w):
        """Drain stored data. Returns False if the strap went silent part-way.

        A link can be "connected" yet dead (seen right after a drop): the strap sends
        nothing and the drain would sit out its whole timeout. Give up after
        drain_stall seconds of silence instead; the caller's next write then either
        works or raises, which triggers a reconnect.
        """
        n0 = self.frames
        self.last_frame = time.monotonic()
        task = asyncio.ensure_future(w.drain(timeout=self.drain_timeout))
        quiet = False
        while not task.done():
            await asyncio.wait({task}, timeout=self.tick)
            self.beat()
            quiet = time.monotonic() - self.last_frame > self.drain_stall
            if quiet or self.stop.is_set() or not w.client.is_connected:
                task.cancel()
                break
        self.c.commit()
        if self.frames > n0:
            db.set_state(self.c, "last_ble_sync", time.time())
            log(f"drained {self.frames - n0} stored frames")
        if quiet:
            log(f"strap silent for {self.drain_stall}s during a drain — checking the link")
        return not quiet

    async def _ble(self, coro):
        """Await one Bluetooth step with a time limit (raises TimeoutError past it)."""
        return await asyncio.wait_for(coro, self.link_timeout)

    async def _close(self, w):
        try:
            await self._ble(w.__aexit__(None, None, None))
        except Exception:
            pass

    @contextlib.asynccontextmanager
    async def _link(self, addr):
        """Connect and run the init handshake, each with a time limit; always disconnect."""
        w = self.strap(addr)
        try:
            await self._ble(w.__aenter__())
            w.verbose = False
            await self._ble(w.init())
        except BaseException:
            await self._close(w)
            raise
        try:
            yield w
        finally:
            await self._close(w)

    async def session(self, addr):
        async with self._link(addr) as w:
            await asyncio.sleep(self.settle)
            self.say(f"connected to {addr}")
            sink = collector.raw_sink_for(self.c, skip=(whoop.T_REALTIME,))  # live HR goes to ble_hr

            def counted(ch, inner):
                self.last_frame = time.monotonic()
                if inner[0] != whoop.T_REALTIME:
                    self.frames += 1
                sink(ch, inner)
            w.raw_sink = counted
            w.on_realtime = self.on_realtime
            w.on_battery = self.on_battery
            w.on_event = self.on_event
            self.offset = None
            self.last_rt = time.monotonic()
            next_drain = 0.0
            streaming = nudged = False
            self.reconnect_by = None           # linked: no restart needed
            asleep = 0
            while not self.stop.is_set() and w.client.is_connected:
                self.beat()
                # Events and command replies go into ble_raw without a commit, and the live
                # stream (which commits) pauses while the strap vibrates. An open write would
                # then block the dashboard's and CLI's requests for up to 20 s (seen on the
                # real strap), so close it every tick.
                self.c.commit()
                asleep = self.slept()
                if not asleep:
                    self.track("linked")
                if asleep:
                    log(f"Mac was asleep for {asleep / 60:.0f} min — reconnecting to the strap")
                    break
                due = self.drain_now.is_set() or time.monotonic() >= next_drain
                self.drain_now.clear()
                if due and self.do_drain:          # --no-drain also ignores Resync's request
                    ok = await self.drain(w)
                    if ok:
                        await self.read_batteries(w)
                    # an abandoned drain is retried soon, not a full interval later
                    next_drain = time.monotonic() + (self.drain_every if ok else min(60, self.drain_every))
                    streaming = False              # re-assert the stream after every drain
                if self.stop.is_set():
                    break
                await self.strap_jobs(w)           # queued buzzes, the strap's alarm
                if not streaming:
                    await self._ble(w.realtime(True))
                    streaming, nudged = True, False
                    self.last_rt = time.monotonic()
                quiet = time.monotonic() - self.last_rt
                if quiet > self.stall:
                    log(f"no live data for {int(quiet)}s — reconnecting")
                    break
                if quiet > self.stall / 3 and not nudged:
                    await self._ble(w.realtime(True))   # one nudge before giving up on the link
                    nudged = True
                elif quiet < self.tick * 2:
                    nudged = False
                await self.nap(self.tick, wake_on_drain=True)
            # after a wake the next link must come quickly; otherwise allow idle_restart
            self.reconnect_by = time.monotonic() + (self.wake_grace if asleep else self.idle_restart)
            try:
                await self._ble(w.realtime(False))
            except Exception:
                pass

    async def nap(self, secs, wake_on_drain=False):
        waits = [asyncio.ensure_future(self.stop.wait())]
        if wake_on_drain:
            waits.append(asyncio.ensure_future(self.drain_now.wait()))
        await asyncio.wait(waits, timeout=secs, return_when=asyncio.FIRST_COMPLETED)
        for t in waits:
            t.cancel()

    # ---------------- housekeeping (no Bluetooth) ----------------
    async def housekeeping(self):
        while not self.stop.is_set():
            for name, every, fn in self.jobs:
                key = f"daemon_last_{name}"
                try:
                    if time.time() - float(db.get_state(self.c, key, 0)) < every:
                        continue
                    ok = await asyncio.to_thread(fn)
                    # a job that failed is tried again in 5 minutes, not a full interval later
                    late = 0 if ok is not False else max(0, every - self.retry_job)
                    db.set_state(self.c, key, time.time() - late)
                except Exception as ex:
                    log(f"{name} error:", ex)
                if self.stop.is_set():
                    return
            await self.nap(30)

    # ---------------- main loop ----------------
    async def run(self):
        holder = collector.lock_holder()
        if holder:
            log(f"another streamer (pid {holder}) already holds the strap — not starting")
            return 1
        with open(LOCK, "w") as f:
            f.write(f"{os.getpid()} daemon")
        self.c = db.connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        loop.add_signal_handler(signal.SIGUSR1, self.drain_now.set)
        log(f"daemon up (pid {os.getpid()}), db {db.DB_PATH}")
        chores = asyncio.ensure_future(self.housekeeping())
        if self.watchdog:
            threading.Thread(target=self._watch, name="watchdog", daemon=True).start()
        wait = self.retry_min
        try:
            while not self.stop.is_set():
                self.beat()
                t0 = time.monotonic()
                if self.slept():
                    wait = self.retry_min          # look right away, and give it wake_grace
                    self.reconnect_by = min(self.reconnect_by or t0 + self.wake_grace,
                                            t0 + self.wake_grace)
                self.track("searching")
                if self.reconnect_by and t0 > self.reconnect_by:
                    self.reconnect_by = t0 + self.idle_restart   # if restart() returns (tests)
                    self.restart("no strap link since the Mac woke or for 30 min")
                    if self.stop.is_set():
                        break
                try:
                    addr = self.addr() or await self._or_stop(asyncio.wait_for(self.find(), self.find_timeout))
                    if addr:
                        await self.session(addr)
                        if not self.stop.is_set():
                            self.say("strap disconnected — looking for it again")
                    else:
                        self.say("strap not in range — will connect when it shows up")
                except Exception as ex:
                    self.say(f"strap not reachable ({ex or type(ex).__name__}) — retrying")
                if self.reconnect_by is None:      # left a session: count from now
                    self.reconnect_by = time.monotonic() + self.idle_restart
                self.mark_offline()
                self.track("searching")
                # a link that lasted resets the backoff; quick failures stretch it
                wait = self.retry_min if time.monotonic() - t0 > 60 else min(wait * 2, self.retry_max)
                await self.nap(wait)
        finally:
            chores.cancel()
            self._close_seg()
            self.mark_offline()
            try:
                os.remove(LOCK)
            except OSError:
                pass
            log("daemon stopped")
        return 0


def main():
    ap = argparse.ArgumentParser(description="Always-on WHOOP strap daemon")
    ap.add_argument("--drain-every", type=int, default=300,
                    help="seconds between drains of stored data while connected (default 300)")
    ap.add_argument("--no-drain", action="store_true",
                    help="live stream only; never drain (and trim) the strap's stored history")
    ap.add_argument("--no-api", action="store_true", help="skip the hourly WHOOP API sync")
    ap.add_argument("--no-cloud", action="store_true",
                    help="skip the hourly cloud push (use when a separate cloud_sync job runs)")
    a = ap.parse_args()
    d = Daemon(drain_every=a.drain_every, do_drain=not a.no_drain, do_api=not a.no_api,
               do_cloud=not a.no_cloud)
    sys.exit(asyncio.run(d.run()))


if __name__ == "__main__":
    main()
