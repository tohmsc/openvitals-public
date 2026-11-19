#!/usr/bin/env python3
"""Tests for the always-on daemon, using a fake strap (no Bluetooth, no network).

Run:  .venv/bin/python -m unittest test_daemon -v
"""
import asyncio, json, os, struct, tempfile, time, types, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, whoop, collector, daemon, decode, alarm  # noqa: E402

collector.LIVE_LOCK = daemon.LOCK = os.path.join(_TMP, ".live.lock")


def console(text, rtc=1):
    """A CONSOLE_LOGS frame as the strap sends it: 3 header + 7 prefix bytes + text."""
    return bytes([0x32, 0, 0]) + struct.pack("<I", rtc) + b"\x00\x00\x00" + text


def metadata(sub, trim=0):
    return bytes([0x31, 0, sub]) + b"\x00" * 10 + struct.pack("<I", trim)


class FakeStrap:
    """Stands in for whoop.Whoop: emits 1 Hz-style live frames and a drain batch."""
    made = []
    hrs = [61, 0, 62, 63]            # one off-wrist zero in the stream
    drop_after = None                # live frames before the link "drops"

    def __init__(self, addr):
        self.addr = addr
        self.client = types.SimpleNamespace(is_connected=True)
        self.on_realtime = self.raw_sink = self.on_battery = self.on_event = None
        self.calls = []
        self.drains = self.sent = 0
        self.realtime_on = 0
        self._task = None
        FakeStrap.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        if self._task:
            self._task.cancel()

    async def init(self):
        pass

    async def realtime(self, on=True):
        if on:
            self.realtime_on += 1
            if not self._task:
                self._task = asyncio.ensure_future(self._emit())

    async def _emit(self):
        while self.client.is_connected:
            hr = self.hrs[self.sent % len(self.hrs)]
            self.raw_sink(whoop.CH_DATA, bytes([whoop.T_REALTIME]) + b"\x00" * 19)
            self.on_realtime({"ts": 31_500_000, "hr": hr, "rr_ms": [1024, 0]})   # strap RTC (whole seconds)
            self.sent += 1
            if self.drop_after and self.sent >= self.drop_after:
                self.client.is_connected = False
            await asyncio.sleep(0.005)

    async def battery(self):
        self.on_battery(88.5)

    async def pack_battery(self):
        return 56

    # ---- haptics and alarm, answering like the real 4.0 did ----
    alarm_works = True               # False: the strap answers SET_ALARM but never stores it
    strap_alarm = None               # what the strap holds (unix seconds)
    buzzes = []                      # (pattern, loops) of every buzz, across all fake straps

    async def buzz(self, pattern=2, loops=1):
        FakeStrap.buzzes.append((pattern, loops))
        if self.on_event:
            self.on_event(whoop.E_HAPTICS_FIRED, bytes([0x30, 0, whoop.E_HAPTICS_FIRED]))
        return {"op": whoop.C_RUN_HAPTICS, "status": 2, "ok": False, "body": b""}

    async def stop_buzz(self):
        return {"op": whoop.C_STOP_HAPTICS, "status": 1, "ok": True, "body": b""}

    async def set_alarm(self, epoch):
        self.calls.append(("set", epoch))
        if self.alarm_works:
            FakeStrap.strap_alarm = int(epoch)
        return {"op": whoop.C_SET_ALARM, "status": 1, "ok": True, "body": b""}

    async def get_alarm(self):
        self.calls.append(("get",))
        return FakeStrap.strap_alarm

    async def disable_alarm(self):
        self.calls.append(("disable",))
        FakeStrap.strap_alarm = None
        return {"op": whoop.C_DISABLE_ALARM, "status": 1, "ok": True, "body": b""}

    async def run_alarm(self):
        self.calls.append(("ring",))
        return {"op": whoop.C_RUN_ALARM, "status": 1, "ok": True, "body": b""}

    async def drain(self, timeout=90):
        self.drains += 1
        self.raw_sink(whoop.CH_DATA, metadata(1))
        self.raw_sink(whoop.CH_DATA, console(b"32, 100: SUPERVISOR: SOC report: 57.25. "))
        self.raw_sink(whoop.CH_DATA, metadata(2, trim=7))
        self.raw_sink(whoop.CH_DATA, metadata(3))
        return True


def make(**kw):
    d = daemon.Daemon(strap=FakeStrap, find=None, addr=lambda: "FAKE-ADDR", **kw)
    d.jobs = []
    d.settle, d.tick, d.retry_min, d.retry_max = 0, 0.01, 0.01, 0.02
    return d


async def run_for(d, secs, during=None):
    task = asyncio.ensure_future(d.run())
    if during:
        await during(d)
    await asyncio.sleep(secs)
    d.stop.set()
    return await asyncio.wait_for(task, 5)


class DaemonTest(unittest.TestCase):
    def setUp(self):
        FakeStrap.made = []
        FakeStrap.drop_after = None
        FakeStrap.buzzes = []
        FakeStrap.strap_alarm = None
        FakeStrap.alarm_works = True
        c = db.connect()
        for t in ("ble_hr", "ble_raw", "clock_sync", "sync_state", "host_log", "strap_cmd"):
            c.execute(f"DELETE FROM {t}")
        c.commit()

    def test_drains_then_streams_live(self):
        d = make()
        self.assertEqual(asyncio.run(run_for(d, 0.3)), 0)
        c = db.connect()
        s = FakeStrap.made[0]
        self.assertEqual(s.drains, 1)                       # drained once on connect
        hrs = [r["hr"] for r in c.execute("SELECT hr FROM ble_hr")]
        self.assertGreater(len(hrs), 5)
        self.assertNotIn(0, hrs)                            # off-wrist zeros are not stored
        ptypes = [r["ptype"] for r in c.execute("SELECT ptype FROM ble_raw")]
        self.assertEqual(sorted(ptypes), [0x31, 0x31, 0x31, 0x32])   # drain stored, live frames not
        clk = c.execute("SELECT rtc, offset FROM clock_sync").fetchall()
        self.assertEqual(len(clk), 1)                       # one clock mapping per connect
        self.assertEqual(clk[0]["rtc"], 31_500_000)
        self.assertIsNotNone(db.get_state(c, "last_ble_sync"))
        self.assertIn('"hr": null', db.get_state(c, "live_hr_now"))  # marked offline on stop
        self.assertFalse(os.path.exists(daemon.LOCK))       # lock released

    def test_redrains_on_interval_and_on_request(self):
        d = make(drain_every=0.05)
        asyncio.run(run_for(d, 0.3))
        self.assertGreaterEqual(FakeStrap.made[0].drains, 3)

        FakeStrap.made = []
        d = make(drain_every=3600)

        async def ask(d):
            await asyncio.sleep(0.1)
            d.drain_now.set()                               # what SIGUSR1 / Resync does
        asyncio.run(run_for(d, 0.2, during=ask))
        s = FakeStrap.made[0]
        self.assertEqual(s.drains, 2)
        self.assertEqual(s.realtime_on, 2)                  # stream re-asserted after each drain

    def test_no_drain_leaves_history_on_the_strap(self):
        d = make(do_drain=False)

        async def ask(d):
            await asyncio.sleep(0.05)
            d.drain_now.set()
        asyncio.run(run_for(d, 0.2, during=ask))
        self.assertEqual(FakeStrap.made[0].drains, 0)
        c = db.connect()
        self.assertGreater(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 5)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ble_raw").fetchone()["n"], 0)

    # ---- buzz and alarm requests from other programs ----
    def test_runs_a_queued_buzz(self):
        c = db.connect()
        rid = alarm.request(c, "buzz", {"pattern": 2, "loops": 1})   # what the dashboard does
        asyncio.run(run_for(make(), 0.2))
        self.assertEqual(FakeStrap.buzzes, [(2, 1)])                 # run once, not every tick
        self.assertEqual(alarm.result(c, rid)["status"], "done")
        self.assertIn("haptics fired", db.get_state(c, alarm.LAST))  # the strap's event is kept

    def test_buzz_queued_while_the_daemon_runs(self):
        async def ask(d):
            await asyncio.sleep(0.1)
            alarm.request(db.connect(), "buzz", {"pattern": 2, "loops": 2})
        asyncio.run(run_for(make(), 0.2, during=ask))
        self.assertEqual(FakeStrap.buzzes, [(2, 2)])

    def test_other_programs_can_queue_while_the_live_stream_pauses(self):
        """The strap pauses live HR while it vibrates but still sends events. Those go into
        ble_raw; the daemon must not keep that write open, or the dashboard's request waits."""
        class EventsOnly(FakeStrap):
            async def _emit(self):
                while self.client.is_connected:
                    self.raw_sink(whoop.CH_EVENT, bytes([whoop.T_EVENT, 0, whoop.E_HAPTICS_ENDED, 0]))
                    await asyncio.sleep(0.05)
        d = make()
        d.strap = EventsOnly
        got = []

        def insert():
            import sqlite3
            c = sqlite3.connect(db.DB_PATH, timeout=0.5)
            try:
                c.execute("INSERT INTO strap_cmd(ts,kind) VALUES(?, 'stop')", (time.time(),))
                c.commit()
                got.append("ok")
            except sqlite3.OperationalError as ex:
                got.append(str(ex))

        async def ask(d):
            await asyncio.sleep(0.15)
            await asyncio.to_thread(insert)
        asyncio.run(run_for(d, 0.3, during=ask))
        self.assertEqual(got, ["ok"])

    def test_a_stale_buzz_is_dropped_not_run_late(self):
        c = db.connect()
        c.execute("INSERT INTO strap_cmd(ts,kind,arg) VALUES(?,?,?)",
                  (time.time() - alarm.BUZZ_TTL - 5, "buzz", "{}"))
        c.commit()
        asyncio.run(run_for(make(), 0.15))
        self.assertEqual(FakeStrap.buzzes, [])
        self.assertEqual(c.execute("SELECT status FROM strap_cmd").fetchone()["status"], "expired")

    def test_a_bad_request_does_not_stop_the_stream(self):
        c = db.connect()
        alarm.request(c, "reboot")                                   # not something we send
        asyncio.run(run_for(make(), 0.2))
        self.assertEqual(len(FakeStrap.made), 1)
        self.assertEqual(c.execute("SELECT status FROM strap_cmd").fetchone()["status"], "failed")
        self.assertGreater(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 5)

    def test_arms_the_strap_alarm_and_clears_it(self):
        c = db.connect()
        alarm.set_alarm(c, "07:00")
        asyncio.run(run_for(make(), 0.15))
        want = alarm.desired(alarm.get(c))
        self.assertEqual(FakeStrap.strap_alarm, want)
        self.assertTrue(alarm.status(c)["on_strap"])
        calls = FakeStrap.made[0].calls
        self.assertEqual(calls.count(("set", want)), 1)              # armed once, then left alone

        async def off(d):
            await asyncio.sleep(0.05)
            alarm.turn_off(db.connect())
        asyncio.run(run_for(make(), 0.15, during=off))
        self.assertIsNone(FakeStrap.strap_alarm)
        self.assertFalse(alarm.status(c)["enabled"])

    def test_rearms_for_tomorrow_after_the_alarm_rang(self):
        c = db.connect()
        alarm.set_alarm(c, "07:00")
        past = time.time() - 60                                      # today's alarm already rang
        db.set_state(c, alarm.ARMED, json.dumps({"epoch": past, "ok": True, "ts": past}))
        asyncio.run(run_for(make(), 0.15))
        self.assertEqual(FakeStrap.strap_alarm, alarm.desired(alarm.get(c)))
        self.assertGreater(FakeStrap.strap_alarm, time.time())

    def test_daemon_rings_itself_if_the_strap_does_not_keep_the_alarm(self):
        FakeStrap.alarm_works = False
        c = db.connect()
        db.set_state(c, alarm.KEY, json.dumps({"enabled": True, "time": "07:00", "daily": False,
                                                "epoch": time.time() + 0.1}))
        asyncio.run(run_for(make(), 0.4))
        self.assertFalse(alarm.status(c, now=time.time() - 1)["on_strap"])
        calls = FakeStrap.made[0].calls
        self.assertEqual(calls.count(("ring",)), 1)                  # rang once at the time

    def test_reconnects_after_the_link_drops(self):
        FakeStrap.drop_after = 5
        d = make()
        asyncio.run(run_for(d, 0.5))
        self.assertGreaterEqual(len(FakeStrap.made), 3)     # kept coming back
        self.assertTrue(all(s.drains == 1 for s in FakeStrap.made[:-1]))   # drains on every connect
        c = db.connect()
        self.assertGreaterEqual(c.execute("SELECT COUNT(*) n FROM clock_sync").fetchone()["n"], 3)

    def test_reconnects_when_live_data_stalls(self):
        class Silent(FakeStrap):
            async def _emit(self):
                return
        d = make()
        d.strap, d.stall = Silent, 0.06
        asyncio.run(run_for(d, 0.4))
        self.assertGreaterEqual(len(FakeStrap.made), 2)
        self.assertGreaterEqual(FakeStrap.made[0].realtime_on, 2)   # nudged before giving up

    def test_abandons_a_drain_when_the_strap_goes_silent(self):
        class Hangs(FakeStrap):
            async def drain(self, timeout=90):
                self.drains += 1
                await asyncio.sleep(timeout)                 # connected, but nothing ever arrives
                return False
        d = make(drain_every=3600)
        d.strap, d.drain_stall = Hangs, 0.05
        asyncio.run(run_for(d, 0.4))
        s = FakeStrap.made[0]
        self.assertEqual(len(FakeStrap.made), 1)            # link was fine: no needless reconnect
        self.assertGreaterEqual(s.realtime_on, 1)           # moved on to the live stream quickly
        c = db.connect()
        self.assertGreater(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 5)

    def test_dead_link_after_a_silent_drain_reconnects_fast(self):
        class Dead(FakeStrap):
            async def drain(self, timeout=90):
                self.drains += 1
                await asyncio.sleep(timeout)
            async def realtime(self, on=True):
                raise RuntimeError("Service Discovery has not been performed yet")
        d = make()
        d.strap, d.drain_stall = Dead, 0.03
        asyncio.run(run_for(d, 0.5))
        self.assertGreaterEqual(len(FakeStrap.made), 3)     # noticed within the stall window, kept retrying

    def test_recovers_from_a_scan_that_never_returns(self):
        """After the Mac sleeps, a CoreBluetooth scan can hang forever; the daemon must retry."""
        calls = []

        async def find():
            calls.append(1)
            if len(calls) == 1:
                await asyncio.Event().wait()            # never finishes
            return "FAKE-ADDR"
        d = make()
        d.addr, d.find, d.find_timeout = (lambda: None), find, 0.05
        asyncio.run(run_for(d, 0.3))
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(FakeStrap.made)                 # connected once the scan was retried
        self.assertGreater(FakeStrap.made[-1].sent, 0)

    def test_recovers_from_a_connect_that_never_returns(self):
        class Stuck(FakeStrap):
            closed = 0

            async def __aenter__(self):
                if len(FakeStrap.made) == 1:
                    await asyncio.Event().wait()        # first connect hangs
                return self

            async def __aexit__(self, *a):
                Stuck.closed += 1
                await super().__aexit__(*a)
        d = make()
        d.strap, d.link_timeout = Stuck, 0.05
        asyncio.run(run_for(d, 0.3))
        self.assertGreaterEqual(len(FakeStrap.made), 2)
        self.assertGreater(FakeStrap.made[1].sent, 0)   # second attempt streams
        self.assertGreaterEqual(Stuck.closed, 1)        # the hung link was told to disconnect

    def test_wake_drops_the_old_link_and_reconnects(self):
        """A jump between wall and monotonic time = the Mac slept: reconnect at once."""
        d = make()
        d.restart = lambda why: None

        async def during(d):
            await asyncio.sleep(0.1)
            d._skew -= 3600                                 # as if the Mac had slept an hour
        asyncio.run(run_for(d, 0.2, during))
        self.assertGreaterEqual(len(FakeStrap.made), 2)
        self.assertGreater(FakeStrap.made[-1].sent, 0)      # the fresh link streams

    def test_host_log_says_linked_and_asleep(self):
        d = make()
        d.restart = lambda why: None

        async def during(d):
            await asyncio.sleep(0.1)
            d._skew -= 3600                                 # as if the Mac had slept an hour
        asyncio.run(run_for(d, 0.25, during))
        c = db.connect()
        rows = [dict(r) for r in c.execute("SELECT start, end, state FROM host_log ORDER BY start")]
        nap = [r for r in rows if r["state"] == "asleep"]
        self.assertEqual(len(nap), 1)
        self.assertAlmostEqual(nap[0]["end"] - nap[0]["start"], 3600, delta=5)
        linked = [r for r in rows if r["state"] == "linked"]
        # (the fake sleep is backdated, so only the order around the wake can be checked)
        self.assertTrue(any(r["end"] <= nap[0]["end"] for r in linked))     # linked before the wake...
        self.assertTrue(any(r["start"] >= nap[0]["end"] for r in linked))   # ...and again after it

    def test_a_night_with_the_mac_asleep_is_filled_in_after_the_wake(self):
        """The strap records all night on its own; after the wake the daemon reconnects, drains
        it, and decode fills ble_hr: the night ends up with no gap."""
        import time, metrics
        night = int(time.time()) - 8 * 3600
        N = 900

        class Night(FakeStrap):
            async def drain(self, timeout=90):
                self.drains += 1
                if len(FakeStrap.made) > 1:                  # the link after the wake hands over the night
                    self.raw_sink(whoop.CH_DATA, metadata(1))
                    for s in range(N):
                        self.raw_sink(whoop.CH_DATA, DecodeTest.history(night + s, 50 + s % 7, rr=(1100,)))
                    self.raw_sink(whoop.CH_DATA, metadata(2, trim=9))
                self.raw_sink(whoop.CH_DATA, metadata(3))
                return True
        d = make()
        d.strap = Night
        d.restart = lambda why: None
        c = db.connect()
        c.execute("DELETE FROM host_log"); c.commit()

        async def during(d):
            await asyncio.sleep(0.1)
            d._skew -= 8 * 3600                             # the Mac slept through the night
        asyncio.run(run_for(d, 0.3, during))
        self.assertGreaterEqual(len(FakeStrap.made), 2)
        decode.run(quiet=True)
        cov = metrics.coverage(c, night, night + N)
        self.assertEqual(cov["recorded_s"], N)
        self.assertEqual(cov["gaps"], [])
        self.assertGreaterEqual(int(db.get_state(c, "history_ts")), night + N - 1)

    def test_restarts_if_the_strap_stays_unreachable_after_a_wake(self):
        """The 2026-11-19 failure: after sleep the scan never finds the strap (it is there)."""
        why = []
        d = make()
        d.addr, d.find = (lambda: None), (lambda: asyncio.sleep(0.01))   # scan finds nothing
        d.wake_grace = 0.05

        def restart(reason):
            why.append(reason)
            d.stop.set()                                    # a real restart replaces the process
        d.restart = restart

        async def during(d):
            d._skew -= 3600
        asyncio.run(run_for(d, 0.3, during))
        self.assertEqual(len(why), 1)

    def test_restarts_if_a_wake_during_a_link_is_followed_by_a_blind_scan(self):
        """Found in the live test: the wake was seen inside the session, and the restart
        deadline after it was the 30-minute one, not wake_grace."""
        why = []
        d = make()
        d.wake_grace = 0.05

        def restart(reason):
            why.append(reason)
            d.stop.set()
        d.restart = restart

        async def during(d):
            await asyncio.sleep(0.1)                        # linked and streaming
            d.addr, d.find = (lambda: None), (lambda: asyncio.sleep(0.01))   # scanner now blind
            d._skew -= 3600                                 # ... after a sleep
        asyncio.run(run_for(d, 0.4, during))
        self.assertEqual(len(why), 1)

    def test_no_restart_while_linked_or_out_of_range_briefly(self):
        why = []
        d = make()
        d.restart = why.append
        asyncio.run(run_for(d, 0.2))                        # linked the whole time
        d = make()
        d.addr, d.find = (lambda: None), (lambda: asyncio.sleep(0.01))
        d.restart = why.append
        asyncio.run(run_for(d, 0.2))                        # away, but not for idle_restart
        self.assertEqual(why, [])

    def test_watchdog_restarts_a_stuck_main_loop(self):
        why = []
        d = make()
        d.watchdog = 0.2
        d.restart = why.append

        async def find():                                   # blocks the whole event loop
            import time as _t
            _t.sleep(0.6)
            return None
        d.addr, d.find = (lambda: None), find
        asyncio.run(run_for(d, 0.05))
        self.assertTrue(why and "stuck" in why[0])

    def test_keeps_looking_while_out_of_range(self):
        calls = []

        async def find():
            calls.append(1)
            return None
        d = make()
        d.addr, d.find = (lambda: None), find
        self.assertEqual(asyncio.run(run_for(d, 0.15)), 0)
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(FakeStrap.made, [])

    def test_housekeeping_runs_on_its_own_timers(self):
        ran = []
        d = make()
        d.addr, d.find = (lambda: None), (lambda: asyncio.sleep(0.01))
        d.jobs = [("fast", 0, lambda: ran.append("fast")), ("daily", 86400, lambda: ran.append("daily"))]
        d.nap_real = d.nap

        async def nap(secs, wake_on_drain=False):
            await d.nap_real(min(secs, 0.02), wake_on_drain)
        d.nap = nap
        asyncio.run(run_for(d, 0.2))
        self.assertEqual(ran.count("daily"), 1)             # not again until tomorrow
        self.assertGreaterEqual(ran.count("fast"), 2)

    def test_failed_job_is_retried_soon_not_a_day_later(self):
        ran = []
        d = make()
        d.addr, d.find = (lambda: None), (lambda: asyncio.sleep(0.01))
        d.retry_job = 0.05

        def flaky():                                        # fails once (returns False, as collector.run_* do)
            ran.append(1)
            return len(ran) > 1
        d.jobs = [("backup", 86400, flaky)]
        d.nap_real = d.nap

        async def nap(secs, wake_on_drain=False):
            await d.nap_real(min(secs, 0.02), wake_on_drain)
        d.nap = nap
        asyncio.run(run_for(d, 0.4))
        self.assertEqual(len(ran), 2)                       # tried again after retry_job, then left for a day

    def test_second_streamer_refuses_to_start(self):
        with open(daemon.LOCK, "w") as f:
            f.write(f"{os.getppid()} daemon")               # a live pid that is not us
        real, collector.is_streamer = collector.is_streamer, lambda pid: True   # ...and is a daemon
        try:
            self.assertEqual(asyncio.run(make().run()), 1)
            self.assertEqual(FakeStrap.made, [])
            self.assertTrue(collector.holder_is_daemon())
        finally:
            collector.is_streamer = real
            os.remove(daemon.LOCK)

    def test_lock_naming_an_unrelated_program_is_stale(self):
        # the pid in the lock is alive but is not daemon.py / live.py (pid reused after a crash):
        # it must not block the daemon, and must never be sent a signal
        with open(daemon.LOCK, "w") as f:
            f.write(f"{os.getppid()} daemon")
        self.assertFalse(collector.is_streamer(os.getppid()))
        self.assertIsNone(collector.lock_holder())
        self.assertFalse(os.path.exists(daemon.LOCK))

    def test_stale_lock_is_cleared(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        os.waitpid(child, 0)                                # child is gone: its pid is dead
        with open(daemon.LOCK, "w") as f:
            f.write(f"{child} daemon")
        self.assertIsNone(collector.lock_holder())
        self.assertFalse(os.path.exists(daemon.LOCK))


class DecodeTest(unittest.TestCase):
    LINES = [b"32, %d: SUPERVISOR: SOC report: %d.50. " % (1000 + i, 40 + i) for i in range(12)] + \
            [b"31, 2000: BLE: connected. ", b"32, 2001: Flash: page 12, 34 written. "]

    def frames(self):
        """Log text cut at awkward points, with a marker split across two frames."""
        stream = b"".join(l[:9] + decode.MARK + l[9:] for l in self.LINES)
        cuts = [0, 7, 10, 11, 40, 41, 97, 150, 151, 152, 260, 333, len(stream)]
        return [console(stream[a:b]) for a, b in zip(cuts, cuts[1:]) if b > a]

    def load(self, frames, c):
        for f in frames:
            c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)", (0, "0005", f[0], f.hex()))
        c.commit()

    def snapshot(self, c):
        return ([tuple(r) for r in c.execute("SELECT rtc,subsystem,message FROM device_logs ORDER BY rowid")],
                [tuple(r) for r in c.execute("SELECT rtc,soc_pct FROM device_soc ORDER BY rtc")])

    def test_incremental_matches_full(self):
        c = db.connect()
        c.execute("DELETE FROM ble_raw"); c.execute("DELETE FROM sync_state"); c.commit()
        frames = self.frames()
        for f in frames:                                    # one frame per run: worst case
            self.load([f], c)
            decode.run(quiet=True)
        self.load([console(b"32, 9999: END: flush. ")], c)  # next line arriving completes the last one
        decode.run(quiet=True)
        step = self.snapshot(c)
        decode.run(full=True, quiet=True)
        full = self.snapshot(c)
        self.assertEqual(len(full[0]), len(self.LINES))     # full holds back only the trailing END line
        self.assertEqual(step, full)
        self.assertEqual(len(full[1]), 12)
        decode.run(quiet=True)                              # nothing new: no duplicates
        self.assertEqual(self.snapshot(c), full)

    @staticmethod
    def history(ts, hr, rr=()):
        """A stored-history record (0x2F, version 24): seq, ts, hr and RR at their offsets."""
        d = bytearray(93)
        struct.pack_into("<II", d, 0, ts, ts)
        d[14], d[15] = hr, len(rr)
        struct.pack_into("<4H", d, 16, *(list(rr) + [0, 0, 0, 0])[:4])
        return bytes([0x2F, 24, 5]) + bytes(d)

    def test_history_backfills_only_the_gaps(self):
        c = db.connect()
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        T = 1_795_000_000
        for s in (3, 4, 5):                                  # the live stream covered these seconds
            c.execute("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,?)", (T + s + 0.13, 70, ""))
        db.set_state(c, "cloud_hr_ts", T + 5.13)             # ...and they were already pushed
        frames = [self.history(T + s, 60 + s, rr=(1000,)) for s in range(6)]   # strap stored 0..5, RR in ms
        frames += [self.history(T + 6, 0),                   # off the wrist
                   self.history(31_500_000, 66)]             # clock never set (1971)
        self.load(frames, c)
        decode.run(quiet=True)
        got = [(r["ts"], r["hr"], r["rr_ms"]) for r in c.execute("SELECT * FROM ble_hr WHERE ts < ? ORDER BY ts", (T + 3,))]
        self.assertEqual(got, [(T + 0.0, 60, "1000"), (T + 1.0, 61, "1000"), (T + 2.0, 62, "1000")])
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 6)   # 3 live + 3 backfilled
        self.assertEqual(float(db.get_state(c, "cloud_hr_ts")), T + 5.13)     # the watermark stays...
        self.assertEqual([r["ts"] for r in c.execute("SELECT ts FROM cloud_hr_pending ORDER BY ts")],
                         [T + 0.0, T + 1.0, T + 2.0])                       # ...and exactly these go up
        decode.run(full=True, quiet=True)                    # rebuilding adds nothing twice
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 6)

    def test_a_repeated_log_line_does_not_stop_the_history(self):
        """Night of 18-19 Nov: the strap re-sent a log line after a reconnect, the insert hit the
        live DB's primary key, and every decode run after that failed before the heart rate."""
        c = db.connect()
        c.execute("DROP TABLE IF EXISTS device_logs")
        c.execute("CREATE TABLE device_logs (rtc INTEGER, subsystem TEXT, message TEXT, "
                  "PRIMARY KEY (rtc, subsystem, message))")      # as the live DB has it
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        T = 1_795_050_000
        line = b"31, 2000: BLE: connected. 31, 2001: BLE: ok. "
        self.load([console(line)], c)
        decode.run(quiet=True)
        self.load([console(line), self.history(T, 47, rr=(1300,))], c)   # repeat, then the night
        decode.run(quiet=True)
        self.assertEqual([r["hr"] for r in c.execute("SELECT hr FROM ble_hr")], [47])
        self.assertEqual(int(db.get_state(c, "decode_rowid")),
                         c.execute("SELECT MAX(rowid) m FROM ble_raw").fetchone()["m"])
        c.execute("DROP TABLE device_logs"); c.commit()
        c.executescript(decode.EXTRA)

    def test_a_failing_part_does_not_hold_back_heart_rate(self):
        """Whatever goes wrong with logs, battery or samples, the heart rate still gets in and
        the watermark moves (one stuck pass once held back a whole night)."""
        c = db.connect()
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        c.executescript(decode.EXTRA)
        c.execute("CREATE TRIGGER boom BEFORE INSERT ON device_logs BEGIN SELECT RAISE(ABORT, 'boom'); END")
        c.commit()
        T = 1_795_060_000
        self.load([console(b"31, 2000: BLE: connected. 31, 2001: BLE: ok. "), self.history(T, 52, rr=(1200,))], c)
        try:
            decode.run(quiet=True)
            self.assertEqual([r["hr"] for r in c.execute("SELECT hr FROM ble_hr")], [52])
            self.assertEqual(int(db.get_state(c, "decode_rowid")),
                             c.execute("SELECT MAX(rowid) m FROM ble_raw").fetchone()["m"])
            self.assertEqual(c.execute("SELECT COUNT(*) n FROM device_logs").fetchone()["n"], 0)
        finally:
            c.execute("DROP TRIGGER boom"); c.commit()

    def test_renumbered_raw_frames_are_not_skipped(self):
        """If ble_raw is emptied, SQLite numbers new frames from 1 again, under the watermark."""
        c = db.connect()
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        T = 1_795_070_000
        for s in range(5):
            c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)",
                      (100.0 + s, "0005", 0x2F, self.history(T + s, 60).hex()))
        c.commit()
        decode.run(quiet=True)
        self.assertEqual(int(db.get_state(c, "decode_rowid")), 5)
        c.execute("DELETE FROM ble_raw")                     # a cleanup or a restored copy
        for s in range(2):                                  # new frames get rowid 1 and 2
            c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)",
                      (200.0 + s, "0005", 0x2F, self.history(T + 100 + s, 70).hex()))
        c.commit()
        decode.run(quiet=True)
        self.assertEqual([r["hr"] for r in c.execute("SELECT hr FROM ble_hr WHERE ts >= ?", (T + 100,))], [70, 70])
        self.assertEqual(int(db.get_state(c, "decode_rowid")), 2)
        decode.run(quiet=True)                               # and the normal path is unchanged
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 7)

    def test_replayed_old_records_fill_only_their_gaps(self):
        """19 Nov 2026 the strap started over from the start of its memory and re-sent records
        from June 2025 (older sequence numbers, real timestamps). They land where they belong,
        once, without moving 'how far history has been read' back or re-sending the cloud."""
        c = db.connect()
        c.executescript(decode.EXTRA)
        for t in ("ble_raw", "ble_hr", "sync_state", "cloud_hr_pending"):
            c.execute(f"DELETE FROM {t}")
        NOW, OLD = 1_795_075_000, 1_749_703_482            # 19 Nov 2026, 12 Jun 2025
        self.load([self.history(NOW + s, 70) for s in range(3)], c)
        decode.run(quiet=True)
        db.set_state(c, "cloud_hr_ts", NOW + 2)              # pushed already
        replay = [self.history(OLD + s, 50) for s in range(4)] + [self.history(NOW + 1, 99)]
        self.load(replay, c)
        decode.run(quiet=True)
        self.load(replay, c)                                 # and the same replay once more
        decode.run(quiet=True)
        got = [(r["ts"], r["hr"]) for r in c.execute("SELECT ts,hr FROM ble_hr ORDER BY ts")]
        self.assertEqual(got, [(OLD + s, 50) for s in range(4)] + [(NOW + s, 70) for s in range(3)])
        self.assertEqual(int(db.get_state(c, "history_ts")), NOW + 2)
        self.assertEqual(float(db.get_state(c, "cloud_hr_ts")), NOW + 2)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM cloud_hr_pending").fetchone()["n"], 4)

    def test_late_heart_rate_is_pushed_once(self):
        import cloud_sync
        c = db.connect()
        c.executescript(decode.EXTRA)
        for t in ("ble_hr", "cloud_hr_pending"):
            c.execute(f"DELETE FROM {t}")
        c.executemany("INSERT INTO ble_hr(ts,hr,rr_ms) VALUES(?,?,'')", [(10.0, 50), (11.0, 51), (99.0, 60)])
        c.executemany("INSERT INTO cloud_hr_pending(ts) VALUES(?)", [(10.0,), (11.0,), (12.0,)])  # 12: gone since
        c.commit()
        sent = []
        d1 = types.SimpleNamespace(upsert=lambda table, cols, rows, chunk=100: sent.extend((r["ts"], r["hr"]) for r in rows))
        self.assertEqual(cloud_sync.push_late_hr(c, d1, batch=2), 2)
        self.assertEqual(sent, [(10.0, 50), (11.0, 51)])
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM cloud_hr_pending").fetchone()["n"], 0)
        self.assertEqual(cloud_sync.push_late_hr(c, d1), 0)

    def test_other_record_versions_are_not_read_as_heart_rate(self):
        # version 25 is a block of raw sensor samples: byte 14 there is not a heart rate
        c = db.connect()
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        T = 1_794_800_000
        other = bytearray(self.history(T, 138)); other[1] = 25          # would pass as "138 bpm"
        junk_rr = bytearray(self.history(T + 1, 70)); junk_rr[3 + 15] = 255   # beat count "255": not a count
        self.load([bytes(other), bytes(junk_rr)], c)
        decode.run(quiet=True)
        self.assertEqual([tuple(r) for r in c.execute("SELECT ts,hr,rr_ms FROM ble_hr")], [(T + 1.0, 70, "")])
        self.assertEqual(int(db.get_state(c, "history_ts")), T + 1)     # how far history has been read

    def test_two_records_in_one_second_are_both_kept(self):
        # the strap ticks slightly faster than 1 Hz, so some seconds carry two records
        c = db.connect()
        for t in ("ble_raw", "ble_hr", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        T = 1_794_900_000
        self.load([self.history(T, 60, rr=(1024,)), self.history(T, 61, rr=(1000,)), self.history(T + 1, 62)], c)
        decode.run(quiet=True)
        got = [(r["ts"], r["hr"]) for r in c.execute("SELECT ts,hr FROM ble_hr ORDER BY ts")]
        self.assertEqual(got, [(T + 0.0, 60), (T + 0.5, 61), (T + 1.0, 62)])
        decode.run(full=True, quiet=True)                    # and a rebuild doesn't add them again
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ble_hr").fetchone()["n"], 3)

    def test_battery_order_survives_a_strap_reboot(self):
        # the firmware tick counter restarts at reboot: the newest report then has the smallest rtc
        c = db.connect()
        for t in ("ble_raw", "sync_state"):
            c.execute(f"DELETE FROM {t}")
        self.load([console(b"32, 900000: SUPERVISOR: SOC report: 40.00. 32, 900001: X: y. ")], c)
        decode.run(quiet=True)
        self.load([console(b"32, 120: SUPERVISOR: SOC report: 95.00. 32, 121: X: y. ")], c)   # after reboot + charge
        decode.run(quiet=True)
        order = [r["soc_pct"] for r in c.execute("SELECT soc_pct FROM device_soc ORDER BY COALESCE(seen,0), rtc")]
        self.assertEqual(order[-2:], [40.0, 95.0])

    def test_log_lines_never_set_the_battery(self):
        # a history drain replays months-old console logs (seen: 47% from June 2025 arriving
        # while the strap sat at 100% on the charger), and a cut line read "SOC report: 9 "
        import metrics
        c = db.connect()
        for t in ("ble_raw", "sync_state", "device_soc"):
            c.execute(f"DELETE FROM {t}")
        self.load([console(b"32, 2330008419: SUPERVISOR: SOC report: 47.27. 32, 80747375: SUPERVISOR: SOC report: 9 "
                           b"31, 80747380: BLE: x. 32, 2: X: y. ")], c)
        decode.run(quiet=True)
        self.assertEqual([r["soc_pct"] for r in c.execute("SELECT soc_pct FROM device_soc")], [47.27])
        self.assertIsNone(metrics.battery(c))
        db.set_state(c, "battery_now", json.dumps({"soc": 47.27, "ts": time.time()}))   # old decode.py
        self.assertIsNone(metrics.battery(c))


class BatteryTest(unittest.TestCase):
    def test_parses_the_strap_reply(self):
        # captured 2026-11-19 on the charger: 24 <seq> 1a | 00 01 e8 03 ...
        self.assertEqual(whoop.parse_battery(bytes.fromhex("24001a0001e80300000000")), 100.0)
        self.assertEqual(whoop.parse_battery(bytes.fromhex("24001a0001d401")), 46.8)
        self.assertIsNone(whoop.parse_battery(bytes.fromhex("24001a00")))
        self.assertIsNone(whoop.parse_battery(bytes.fromhex("24001a0001ffff")))

    def test_daemon_stores_the_strap_reading(self):
        import metrics
        FakeStrap.made = []
        d = make()
        asyncio.run(run_for(d, 0.3))
        self.assertEqual(metrics.battery(db.connect()), 88.5)
        self.assertEqual(metrics.pack_battery(db.connect()), 56)

    def test_a_failed_pack_read_keeps_the_link(self):
        import metrics

        class NoPack(FakeStrap):
            async def pack_battery(self):
                raise OSError("characteristic not found")
        FakeStrap.made = []
        d = daemon.Daemon(strap=NoPack, find=None, addr=lambda: "FAKE-ADDR")
        d.jobs = []
        d.settle, d.tick, d.retry_min, d.retry_max = 0, 0.01, 0.01, 0.02
        asyncio.run(run_for(d, 0.3))
        self.assertEqual(len(FakeStrap.made), 1)            # one link, never dropped
        self.assertEqual(metrics.battery(db.connect()), 88.5)


if __name__ == "__main__":
    unittest.main()
