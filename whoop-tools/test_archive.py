#!/usr/bin/env python3
"""Tests for archive.py with a fake bucket (a folder). No network, no Cloudflare.

Run:  .venv/bin/python -m unittest test_archive -v
"""
import os, shutil, sqlite3, tempfile, time, unittest

_TMP = tempfile.mkdtemp(prefix="whoop-archive-test-")
os.environ["WHOOP_DB"] = os.path.join(_TMP, "test.db")

import db, archive, decode  # noqa: E402


class FolderStore:
    """Stands in for R2: objects are files in a folder."""
    def __init__(self):
        self.dir = tempfile.mkdtemp(dir=_TMP)
        self.puts = []
        self.fail_put = self.corrupt = False

    def _p(self, key):
        return os.path.join(self.dir, key.replace("/", "__"))

    def put(self, key, path):
        if self.fail_put:
            raise RuntimeError("upload refused")
        shutil.copyfile(path, self._p(key))
        self.puts.append(key)

    def get(self, key, path):
        shutil.copyfile(self._p(key), path)
        if self.corrupt:
            with open(path, "r+b") as f:
                f.seek(10); f.write(b"\x00\x00\x00")


HOUR = 3600
NOW_HOUR = int(time.time() // HOUR)


def fill(c, hours_ago, n, start=0):
    """n raw frames spread through the hour that began `hours_ago` hours before this one."""
    base = (NOW_HOUR - hours_ago) * HOUR
    for i in range(n):
        c.execute("INSERT INTO ble_raw(ts,channel,ptype,hex) VALUES(?,?,?,?)",
                  (base + 10 + i, "0005", 0x2F, f"{start + i:08x}"))
    c.commit()


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.c = db.connect()
        for t in ("ble_raw", "raw_archive", "sync_state"):
            self.c.execute(f"DELETE FROM {t}")
        self.c.commit()
        archive.KEY = os.path.join(_TMP, "no-key")           # unencrypted unless a test sets one
        self.store = FolderStore()

    def count(self):
        return self.c.execute("SELECT COUNT(*) n FROM ble_raw").fetchone()["n"]

    def decoded_all(self):
        db.set_state(self.c, "decode_rowid", self.c.execute("SELECT MAX(rowid) m FROM ble_raw").fetchone()["m"])

    def test_moves_old_hours_and_keeps_the_window(self):
        fill(self.c, 30, 50); fill(self.c, 29, 40, 50); fill(self.c, 2, 30, 90); fill(self.c, 0, 5, 120)
        self.decoded_all()
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 2)
        self.assertEqual(self.count(), 35)                    # last 24 h stay local
        rows = self.c.execute("SELECT hour, rows, first_rowid, last_rowid FROM raw_archive ORDER BY hour").fetchall()
        self.assertEqual([(r["hour"], r["rows"]) for r in rows], [(NOW_HOUR - 30, 50), (NOW_HOUR - 29, 40)])
        self.assertEqual(len(self.store.puts), 2)
        self.assertTrue(self.store.puts[0].startswith("raw/"))
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 0)   # nothing left to do

    def test_failed_upload_deletes_nothing(self):
        fill(self.c, 30, 50); fill(self.c, 0, 5, 50)
        self.decoded_all()
        self.store.fail_put = True
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 0)
        self.assertEqual(self.count(), 55)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM raw_archive").fetchone()["n"], 0)

    def test_corrupt_copy_deletes_nothing(self):
        fill(self.c, 30, 50); fill(self.c, 0, 5, 50)
        self.decoded_all()
        self.store.corrupt = True
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 0)
        self.assertEqual(self.count(), 55)
        self.store.corrupt = False                            # next run succeeds
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 1)
        self.assertEqual(self.count(), 5)

    def test_never_moves_undecoded_frames(self):
        fill(self.c, 30, 50)
        self.decoded_all()                                    # decoder has read hour -30 only
        fill(self.c, 29, 40, 50); fill(self.c, 0, 5, 90)
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 1)
        self.assertEqual(self.count(), 45)

    def test_never_empties_the_table(self):
        fill(self.c, 30, 50)                                  # only old rows, e.g. laptop was off for days
        self.decoded_all()
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 0)
        self.assertEqual(self.count(), 50)                    # kept, so rowids keep counting upward
        fill(self.c, 0, 1, 50)                                # a new frame arrives ...
        self.decoded_all()
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 1)   # ... now the old hour can go
        new = self.c.execute("SELECT rowid FROM ble_raw").fetchall()
        self.assertEqual([r["rowid"] for r in new], [51])

    def test_encrypted_round_trip_restores_every_row(self):
        archive.KEY = os.path.join(_TMP, "key")
        with open(archive.KEY, "w") as f:
            f.write("test-key-not-a-real-secret\n")
        fill(self.c, 30, 50); fill(self.c, 29, 40, 50); fill(self.c, 0, 5, 90)
        self.decoded_all()
        want = [tuple(r) for r in self.c.execute(
            "SELECT rowid,ts,channel,ptype,hex FROM ble_raw WHERE ts < ? ORDER BY rowid", ((NOW_HOUR - 24) * HOUR,))]
        self.assertEqual(archive.run(self.store, keep_hours=24, quiet=True), 2)
        self.assertTrue(all(k.endswith(".enc") for k in self.store.puts))
        blob = open(self.store._p(self.store.puts[0]), "rb").read()
        self.assertTrue(blob.startswith(b"Salted__"))         # really encrypted, not just renamed
        out = os.path.join(_TMP, "restored.db")
        if os.path.exists(out):
            os.remove(out)
        self.assertEqual(archive.restore(out, store=self.store), 90)
        got = [tuple(r) for r in sqlite3.connect(out).execute("SELECT rowid,ts,channel,ptype,hex FROM ble_raw ORDER BY rowid")]
        self.assertEqual(got, want)
        only = os.path.join(_TMP, "one-hour.db")
        if os.path.exists(only):
            os.remove(only)
        self.assertEqual(archive.restore(only, start=NOW_HOUR - 29, end=NOW_HOUR - 28, store=self.store), 40)

    def test_full_decode_is_refused_once_frames_are_archived(self):
        fill(self.c, 30, 50); fill(self.c, 0, 5, 50)
        self.decoded_all()
        self.c.executescript(decode.EXTRA)
        self.c.execute("INSERT INTO device_logs(rtc,subsystem,message) VALUES(1,'BLE','from an archived frame')")
        self.c.commit()
        archive.run(self.store, keep_hours=24, quiet=True)
        decode.run(full=True, quiet=True)                     # refused: would lose that log line
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM device_logs").fetchone()["n"], 1)
        decode.run(full=True, quiet=True, force=True)
        self.assertEqual(self.c.execute("SELECT COUNT(*) n FROM device_logs").fetchone()["n"], 0)

    def test_new_frames_still_decode_after_pruning(self):
        fill(self.c, 30, 50); fill(self.c, 0, 5, 50)
        self.c.executescript(decode.EXTRA)
        decode.run(quiet=True)
        mark = int(db.get_state(self.c, "decode_rowid"))
        archive.run(self.store, keep_hours=24, quiet=True)
        fill(self.c, 0, 3, 200)
        decode.run(quiet=True)
        self.assertEqual(int(db.get_state(self.c, "decode_rowid")), mark + 3)   # picked up, not skipped


if __name__ == "__main__":
    unittest.main()
