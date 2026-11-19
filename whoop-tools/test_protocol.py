import os, tempfile, time, unittest

os.environ["WHOOP_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import asyncio, db, whoop, struct


class AckTest(unittest.TestCase):
    def test_history_end_ack_echoes_the_strap_block(self):
        # a HISTORY_END the strap sent on 2026-11-19 (inner frame, as stored in ble_raw)
        inner = bytes.fromhex("31e4027e4aff6a70491d0000006796010016000000000000")
        m = whoop.parse_metadata(inner)
        self.assertEqual((m["subtype"], m["trim"]), (2, 0x019667))
        self.assertEqual(m["ack"], bytes.fromhex("6796010016000000"))
        sent = []
        w = whoop.Whoop.__new__(whoop.Whoop)
        async def send(frame): sent.append(frame)
        w.send = send
        asyncio.run(w._ack(m["trim"], m["ack"]))
        (out,), _ = whoop.unframe(sent[0])                     # one valid frame (CRCs check out)
        self.assertEqual(out[2:], bytes([whoop.C_HIST_ACK, 1]) + m["ack"])


class RRUnitsTest(unittest.TestCase):
    def test_strap_rr_is_milliseconds(self):
        inner = bytes([0x28, 0]) + struct.pack("<IH", 1, 0) + bytes([55, 2]) + struct.pack("<4H", 1091, 1100, 0, 0)
        self.assertEqual(whoop.parse_realtime(inner)["rr_ms"], [1091, 1100])   # only rrN of them

    def test_old_rows_rescaled_once_and_queued_for_the_cloud(self):
        c = db.connect()
        c.execute("DELETE FROM ble_hr")
        c.execute("PRAGMA user_version = 0")
        now = time.time()
        # 1091 and 1100 ms as the old code stored them (x * 1000/1024, one decimal)
        c.execute("INSERT INTO ble_hr VALUES(?,?,?)", (now - 60, 55, "1065.4 1074.2"))
        c.execute("INSERT INTO ble_hr VALUES(?,?,?)", (now - 30 * 86400, 56, "1065.4"))
        c.execute("INSERT INTO ble_hr VALUES(?,?,?)", (now - 50, 57, ""))
        c.execute("INSERT OR REPLACE INTO daily_night(date, hrv) VALUES('2026-11-19', 50.0)")
        c.commit()
        self.assertEqual(db.fix_rr_units(c), 2)
        self.assertAlmostEqual(c.execute("SELECT hrv FROM daily_night").fetchone()[0], 51.2)
        got = dict(c.execute("SELECT hr, rr_ms FROM ble_hr").fetchall())
        self.assertEqual(got, {55: "1091 1100", 56: "1091", 57: ""})
        pend = [r[0] for r in c.execute("SELECT ts FROM cloud_hr_pending")]
        self.assertEqual(pend, [now - 60])                        # only the recent row with beats
        self.assertEqual(db.fix_rr_units(c), 0)                   # never twice...
        c.execute("DELETE FROM sync_state"); c.commit()
        self.assertEqual(db.fix_rr_units(c), 0)                   # ...even if sync_state is cleared
        self.assertEqual(c.execute("SELECT rr_ms FROM ble_hr WHERE hr = 55").fetchone()[0], "1091 1100")


if __name__ == "__main__":
    unittest.main()
