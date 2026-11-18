#!/usr/bin/env python3
"""
whoop.py — a from-scratch WHOOP 4.0 client (BLE), independent of the WHOOP app.

Protocol (CRC-verified against real captures):
  Frame: AA | LEN(u16 LE = len(inner)+4) | CRC8(LEN) | inner | CRC32(inner, LE)
  inner = [type u8][seq u8][cmd u8][data...]   (0x28 realtime packs ts in place of cmd)
  CRC8  = CRC-8/SMBUS (poly 0x07, init 0, xorout 0)   over the 2 length bytes
  CRC32 = CRC-32/ISO-HDLC (binascii.crc32)             over inner

Packet types: 0x23 COMMAND 0x24 CMD_RESP 0x28 REALTIME 0x2F HISTORICAL
              0x30 EVENT 0x31 METADATA 0x32 CONSOLE
CLI:  whoop.py info | live | drain
"""
import asyncio, struct, binascii, sys, time, csv, json, os, datetime as dt
from bleak import BleakClient, BleakScanner

WHOOP_SVC = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
BASE = "6108{:04x}-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_CHAR = BASE.format(2)
CH_RESP, CH_EVENT, CH_DATA, CH_7 = (BASE.format(n) for n in (3, 4, 5, 7))
HR_CHAR = "00002a37-0000-1000-8000-00805f9b34fb"
HERE = os.path.dirname(os.path.abspath(__file__))

# ---- packet types ----
T_COMMAND, T_CMD_RESP, T_REALTIME = 0x23, 0x24, 0x28
T_HISTORICAL, T_EVENT, T_METADATA, T_CONSOLE = 0x2F, 0x30, 0x31, 0x32
# ---- command opcodes ----
C_HELLO_HARVARD, C_VERSION, C_TOGGLE_HR = 0x23, 0x07, 0x0E
C_REALTIME_HR, C_SET_CLOCK, C_GET_CLOCK = 0x03, 0x0A, 0x0B
C_ABORT_HIST, C_SEND_HIST, C_HIST_ACK = 0x14, 0x16, 0x17
C_BATTERY, C_GET_NAME, C_HIGHFREQ_ON = 0x1A, 0x4C, 0x60


def crc8(d: bytes) -> int:
    c = 0
    for b in d:
        c ^= b
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def build(ptype: int, seq: int, cmd: int, data: bytes = b"") -> bytes:
    inner = bytes([ptype, seq, cmd]) + data
    ln = struct.pack("<H", len(inner) + 4)
    return b"\xAA" + ln + bytes([crc8(ln)]) + inner + struct.pack("<I", binascii.crc32(inner) & 0xFFFFFFFF)


def cmd(opcode: int, data: bytes = b"", seq: int = 0) -> bytes:
    return build(T_COMMAND, seq, opcode, data)


def unframe(buf: bytes):
    """Split a byte buffer into inner payloads; returns (inners, leftover).

    Resyncs on the next 0xAA whose header CRC8 checks out, so one stray or
    corrupt byte can't stall the channel, and drops frames whose CRC32 fails.
    """
    out = []
    while True:
        i = buf.find(b"\xAA")
        if i < 0:
            return out, b""
        buf = buf[i:]
        if len(buf) < 4:
            break
        ln = struct.unpack_from("<H", buf, 1)[0]      # len(inner) + 4 (crc32)
        if ln < 5 or crc8(buf[1:3]) != buf[3]:        # not a real header
            buf = buf[1:]
            continue
        flen = 4 + ln                                 # AA + len(2) + crc8 + inner + crc32
        if len(buf) < flen:
            break
        inner = buf[4:flen - 4]
        if struct.unpack_from("<I", buf, flen - 4)[0] != binascii.crc32(inner) & 0xFFFFFFFF:
            print(f"  (dropped frame with bad CRC32, {flen} bytes)")
            buf = buf[1:]
            continue
        out.append(inner)
        buf = buf[flen:]
    return out, buf


def parse_realtime(inner: bytes):
    # inner = [0x28][ver][ts u32 LE @2][subsec u16 @6][hr @8][rrN @9][4×u16 RR @10..18]
    if len(inner) < 10:
        return None
    ts = struct.unpack_from("<I", inner, 2)[0]
    hr = inner[8]
    n = min(inner[9], 4, (len(inner) - 10) // 2)      # never read past a short frame
    rr = [struct.unpack_from("<H", inner, 10 + 2 * i)[0] for i in range(n)]
    return {"ts": ts, "hr": hr, "rr_1024": rr}


def parse_historical(inner: bytes):
    # inner=[0x2F][ver][cmd][data...]; data=inner[3:]. One record per second, ts = Unix time.
    # seq/ts/hr/rr are verified on record version 24 (hr and RR match the live stream);
    # the fields after rr come from the V12 layout and are not verified for other versions.
    d = inner[3:]
    if len(d) < 24:
        return {"ver": inner[1], "raw": d.hex()}
    rec = {
        "ver": inner[1],
        "seq": struct.unpack_from("<I", d, 0)[0],
        "ts": struct.unpack_from("<I", d, 4)[0],
        "hr": d[14] if len(d) > 14 else None,
        "rr_count": d[15] if len(d) > 15 else None,
    }
    if len(d) >= 24:
        rec["rr_1024"] = [struct.unpack_from("<H", d, 16 + 2 * i)[0] for i in range(4)]
    if len(d) >= 45:
        rec["accel_g"] = list(struct.unpack_from("<3f", d, 33))
    if len(d) >= 49:
        rec["skin_contact"] = d[48]
    if len(d) >= 77:
        rec["spo2_red"], rec["spo2_ir"] = struct.unpack_from("<2H", d, 61)
        rec["skin_temp_raw"] = struct.unpack_from("<H", d, 65)[0]
    return rec


def parse_metadata(inner: bytes):
    sub = inner[2]
    d = inner[3:]
    trim = struct.unpack_from("<I", d, 10)[0] if len(d) >= 14 else None
    return {"subtype": sub, "trim": trim}  # 1 start, 2 end, 3 complete


class Whoop:
    def __init__(self, address):
        self.address = address
        self.client = BleakClient(address, timeout=20.0)
        self.buffers = {CH_RESP: b"", CH_EVENT: b"", CH_DATA: b"", CH_7: b""}
        self.on_realtime = None
        self.on_historical = None
        self.on_meta = None
        self.raw_sink = None          # optional: called with (channel, inner) for every frame
        self.verbose = True           # print every response/event (the daemon turns this off)
        self._hist_done = asyncio.Event()

    async def __aenter__(self):
        await self.client.connect()
        for ch in (CH_RESP, CH_EVENT, CH_DATA, CH_7):
            await self.client.start_notify(ch, self._mk(ch))
        return self

    async def __aexit__(self, *a):
        try:
            await self.client.disconnect()
        except Exception:
            pass

    def _mk(self, ch):
        def handler(_, data):
            self.buffers[ch] += bytes(data)
            inners, self.buffers[ch] = unframe(self.buffers[ch])
            for inner in inners:
                self._dispatch(ch, inner)
        return handler

    def _dispatch(self, ch, inner):
        if len(inner) < 3:            # too short to carry a type, sequence and command
            return
        if self.raw_sink:
            self.raw_sink(ch, inner)
        t = inner[0]
        if t == T_REALTIME and self.on_realtime:
            r = parse_realtime(inner)
            if r:
                self.on_realtime(r)
        elif t == T_HISTORICAL and self.on_historical:
            self.on_historical(parse_historical(inner))
        elif t == T_METADATA:
            m = parse_metadata(inner)
            if self.on_meta:
                self.on_meta(m)
            if m["subtype"] == 2 and m["trim"] is not None:   # HISTORY_END -> ACK
                asyncio.create_task(self._ack(m["trim"]))
            if m["subtype"] == 3:                              # COMPLETE
                self._hist_done.set()
        elif t == T_CMD_RESP and self.verbose:
            print(f"  [resp op=0x{inner[2]:02x}] {inner[3:].hex(' ')}")
        elif t == T_EVENT and self.verbose:
            print(f"  [event #{inner[2]}] {inner[3:].hex(' ')}")

    async def send(self, frame):
        await self.client.write_gatt_char(CMD_CHAR, frame, response=False)

    async def _ack(self, trim):
        await self.send(cmd(C_HIST_ACK, struct.pack("<BII", 1, trim, 0)))

    async def init(self):
        await self.send(cmd(C_HELLO_HARVARD, b"\x00"))
        await self.set_time()
        await self.send(cmd(C_GET_NAME, b"\x00"))
        await self.send(cmd(C_HIGHFREQ_ON))

    async def set_time(self):
        now = int(time.time())
        await self.send(cmd(C_SET_CLOCK, struct.pack("<I", now) + b"\x00" * 5))

    async def battery(self):
        await self.send(cmd(C_BATTERY, b"\x00"))

    async def version(self):
        await self.send(cmd(C_VERSION, b"\x00"))

    async def hr_broadcast(self, on=True):
        await self.send(cmd(C_TOGGLE_HR, bytes([1 if on else 0])))

    async def realtime(self, on=True):
        await self.send(cmd(C_REALTIME_HR, bytes([1 if on else 0])))

    async def drain(self, timeout=90):
        self._hist_done.clear()
        await self.send(cmd(C_SEND_HIST, b"\x00"))
        try:
            await asyncio.wait_for(self._hist_done.wait(), timeout)
        except asyncio.TimeoutError:
            if self.verbose:
                print("  (drain timed out — may just mean no/!more stored data)")
            return False
        return True


# ------------------------- CLI -------------------------
async def find():
    print("scanning...")
    d = await BleakScanner.find_device_by_filter(
        lambda dev, adv: WHOOP_SVC in (adv.service_uuids or []), timeout=15.0)
    return d.address if d else None


async def cli():
    mode = sys.argv[1] if len(sys.argv) > 1 else "info"
    addr = sys.argv[2] if len(sys.argv) > 2 else await find()
    if not addr:
        print("WHOOP not found (close the phone app — one bond at a time).")
        return
    async with Whoop(addr) as w:
        print("connected", addr)
        await w.init()
        await asyncio.sleep(1.5)

        if mode == "info":
            await w.battery(); await w.version()
            await asyncio.sleep(2)

        elif mode == "live":
            secs = float(sys.argv[3]) if len(sys.argv) > 3 else 60
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            f = open(os.path.join(HERE, f"live_{stamp}.csv"), "w", newline="")
            wr = csv.writer(f); wr.writerow(["iso", "hr", "rr_ms"])
            def rt(r):
                rr = [round(x / 1024 * 1000, 1) for x in r["rr_1024"] if x]
                wr.writerow([dt.datetime.now().isoformat(), r["hr"], " ".join(map(str, rr))]); f.flush()
                print(f"HR {r['hr']:3d}  RR {rr}")
            w.on_realtime = rt
            await w.realtime(True)
            await asyncio.sleep(secs)
            await w.realtime(False)
            f.close(); print(f"saved live_{stamp}.csv")

        elif mode == "drain":
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            raw = open(os.path.join(HERE, f"history_{stamp}.ndjson"), "w")
            n = [0]
            def sink(ch, inner):
                raw.write(json.dumps({"ch": ch[4:8], "type": inner[0], "hex": inner.hex()}) + "\n")
                raw.flush(); n[0] += 1
            def meta(m): print(f"  META subtype={m['subtype']} trim={m['trim']}")
            w.raw_sink = sink; w.on_meta = meta
            await w.drain(timeout=120)
            raw.close()
            print(f"captured {n[0]} raw frames -> history_{stamp}.ndjson")


if __name__ == "__main__":
    # self-test of frame builder against CRC-verified captures
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        checks = {
            "HR on":   (cmd(C_TOGGLE_HR, b"\x01"), "aa0800a823000e01d4c2477a"),
            "HR off":  (cmd(C_TOGGLE_HR, b"\x00"), "aa0800a823000e0042f2400d"),
            "realtime on": (cmd(C_REALTIME_HR, b"\x01"), "aa0800a82300030199bce9cf"),
            "battery": (cmd(C_BATTERY, b"\x00"), "aa0800a823001a001725ee23"),
            "version": (cmd(C_VERSION, b"\x00"), "aa0800a8230007000b4982dc"),
            "hello":   (cmd(C_HELLO_HARVARD, b"\x00"), "aa0800a823002300ada86a2d"),
            "getname": (cmd(C_GET_NAME, b"\x00"), "aa0800a823004c00c5df0fcf"),
            "drain":   (cmd(C_SEND_HIST, b"\x00"), "aa0800a8230016001b6a5b8f"),
            "highfreq":(cmd(C_HIGHFREQ_ON),      "aa07006b230060f340f888"),
            "abort":   (cmd(C_ABORT_HIST),       "aa07006b230014d6f590df"),
        }
        ok = True
        for name, (got, want) in checks.items():
            g = got.hex()
            mark = "OK " if g == want else "FAIL"
            if g != want: ok = False
            print(f"  [{mark}] {name:12s} {g}" + ("" if g == want else f"  != {want}"))
        print("ALL PASS" if ok else "MISMATCH")
    else:
        asyncio.run(cli())
