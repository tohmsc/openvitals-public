import json, os, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["WHOOP_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import db


class McpTest(unittest.TestCase):
    """Talks to mcp_server.py the way a client does: newline-delimited JSON-RPC over stdio."""

    @classmethod
    def setUpClass(cls):
        c = db.connect()
        now = time.time()
        c.executemany("INSERT INTO ble_hr VALUES(?,?,?)", [(now - 120 + s, 60 + s % 5, "1000") for s in range(120)])
        c.execute("INSERT INTO daily_night(date, rhr, hrv, n_rr) VALUES(?, 45, 60, 5000)", (time.strftime("%Y-%m-%d"),))
        c.commit(); c.close()

    def call(self, *msgs):
        p = subprocess.run([sys.executable, os.path.join(HERE, "mcp_server.py")], env=os.environ,
                           input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True, timeout=30)
        return [json.loads(l) for l in p.stdout.splitlines()]

    def tool(self, name, **args):
        (r,) = self.call({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r["result"]

    def test_handshake_and_list(self):
        init, lst = self.call(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},          # no reply to a notification
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["result"]["capabilities"])
        names = {t["name"] for t in lst["result"]["tools"]}
        self.assertEqual(names, {"readiness", "today", "nights", "scores", "heart_rate", "schema", "sql"})

    def test_tools_answer(self):
        for name in ("readiness", "today", "nights", "scores", "schema"):
            r = self.tool(name)
            self.assertFalse(r["isError"], (name, r))
            json.loads(r["content"][0]["text"])
        hr = json.loads(self.tool("heart_rate", hours=1)["content"][0]["text"])
        self.assertEqual(sum(p["n"] for p in hr["points"]), 120)

    def test_sql_is_read_only(self):
        ok = json.loads(self.tool("sql", query="SELECT COUNT(*) n FROM ble_hr")["content"][0]["text"])
        self.assertEqual(ok["rows"], [[120]])
        for bad in ("DELETE FROM ble_hr", "SELECT 1; DELETE FROM ble_hr",
                    "WITH x AS (SELECT 1) DELETE FROM ble_hr"):
            self.assertTrue(self.tool("sql", query=bad)["isError"], bad)
        c = db.connect()
        self.assertEqual(c.execute("SELECT COUNT(*) FROM ble_hr").fetchone()[0], 120)

    def test_unknown_method(self):
        (r,) = self.call({"jsonrpc": "2.0", "id": 9, "method": "nope"})
        self.assertEqual(r["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
