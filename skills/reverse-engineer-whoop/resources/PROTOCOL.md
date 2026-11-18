# WHOOP 4.0 BLE protocol reference (verified)

Verified against real captured frames from a WHOOP 4.0 (firmware Harvard 41.17.x /
Boylston 17.2.x). 5.0/MG differ where noted. Not official; use at your own risk.

## GATT
- Custom service **`61080001-8d6d-82b8-614a-1c8cb0f8dcc6`** (GEN_4 / 4.0). 5.0/MG = `fd4b0001-…`.
  - `…0002` write / write-without-response — **commands host→strap**
  - `…0003` notify — command responses
  - `…0004` notify — events
  - `…0005` notify — data (realtime / historical / metadata / console)
  - `…0007` notify
- Standard services: Heart Rate `0x180D`/`0x2A37` (emits only after custom enable + skin
  contact), Device Info `0x180A` (`WHOOP Inc.`), Battery `0x180F`/`0x2A19`.

## Frame format
```
AA | LEN(u16 LE = len(inner)+4) | CRC8(LEN) | inner | CRC32(inner, u32 LE)
inner = [type u8][seq u8][cmd u8][data...]
```
- **CRC8** = CRC-8/SMBUS (poly 0x07, init 0, xorout 0) over the 2 LEN bytes only.
- **CRC32** = CRC-32/ISO-HDLC = Python `binascii.crc32(inner)` over `inner` only.
  (5.0/MG payload check is CRC16-Modbus instead.)
- `seq` is not validated in commands (use 0). In HISTORICAL frames it is the record version.

```python
import struct, binascii
def crc8(d):
    c=0
    for b in d:
        c^=b
        for _ in range(8): c=((c<<1)^0x07)&0xFF if c&0x80 else (c<<1)&0xFF
    return c
def frame(ptype, seq, cmd, data=b""):
    inner=bytes([ptype,seq,cmd])+data
    ln=struct.pack("<H",len(inner)+4)
    return b"\xAA"+ln+bytes([crc8(ln)])+inner+struct.pack("<I",binascii.crc32(inner)&0xFFFFFFFF)
```

## Packet types (inner[0])
0x23 COMMAND · 0x24 COMMAND_RESPONSE · 0x28 REALTIME_DATA · 0x2B REALTIME_RAW ·
0x2F HISTORICAL_DATA · 0x30 EVENT · 0x31 METADATA · 0x32 CONSOLE_LOGS ·
0x33 REALTIME_IMU · 0x34 HISTORICAL_IMU

## Command opcodes (inner[2] when type=0x23) — selected
| op | name | op | name |
|---|---|---|---|
| 0x03 | ToggleRealtimeHr (1 Hz stream) | 0x07 | ReportVersionInfo |
| 0x0A | SetClock | 0x0B | GetClock |
| 0x0E | ToggleGenericHrProfile (std HR on/off) | 0x14 | AbortHistoricalTransmits |
| 0x16 | SendHistoricalData (drain) | 0x17 | HistoricalDataResult (batch ACK) |
| 0x1A | GetBatteryLevel | 0x1D | RebootStrap |
| 0x21 | SetReadPointer | 0x22 | GetDataRange |
| 0x3F | SendR10R11Realtime (raw optical) | 0x51/0x52 | Start/Stop RawData |
| 0x60/0x61 | Enter/Exit HighFreqSync | 0x6A | ToggleImuMode |

## Verified command frames (hex)
- HR broadcast ON: `aa0800a823000e01d4c2477a` · OFF: `aa0800a823000e0042f2400d`
- Realtime ON: `aa0800a82300030199bce9cf`
- Battery: `aa0800a823001a001725ee23` · Version: `aa0800a8230007000b4982dc`
- Start drain: `aa0800a8230016001b6a5b8f` · Abort drain: `aa07006b230014d6f590df`
- Drain batch ACK (0x17): data = `struct.pack("<BII", 1, trim, 0)` (echo the `trim` from
  the METADATA HISTORY_END frame, else the strap replays the batch)
- Enter HighFreqSync: `aa07006b230060f340f888`
- Init sequence that works: subscribe 0003/0004/0005/0007 (+2A37) → HelloHarvard →
  SetClock(now) → GetName → EnterHighFreqSync.

## Notification parsing
- **REALTIME_DATA (0x28):** `ts u32 LE @2`, `hr @8`, `rr_count @9`, up to 4×`u16 RR @10`
  (RR in 1/1024 s). `ts` is the device RTC counter (not Unix).
- **HISTORICAL_DATA (0x2F):** the strap's own store, one 96-byte record per second,
  `inner[1]` = record version (24 on Harvard 41.17.x), data = `inner[3:]`:
  `seq u32 @0`, `ts u32 @4` (**Unix time**), `hr @14`, `rr_count @15`, 4×`u16 RR @16`
  (1/1024 s, same as realtime). Verified against the live stream: HR agrees within 2 bpm
  over overlapping seconds, and records keep accumulating while nothing is connected.
  They only appeared once the strap's clock read real time; while its RTC sat near 1971
  (after a reset) a drain returned just console logs and sparse events. Not verified: whether
  our SetClock (0x0A) payload is what fixed the clock — six hourly runs sent it with no effect.
  Fields after RR (accel, optical, skin temp) follow the V12 layout and are not verified.
- **HISTORICAL drain:** arrives as METADATA (0x31: subtype 1 START, 2 END+trim@offset10,
  3 COMPLETE) + records. On some firmware the per-sample records come **event-framed**
  (type 0x30, cmd 3 and 63). `cmd 63 = 0x3F = R10/R11 raw optical (PPG)`. Capture raw;
  exact per-field offsets for the event format are not independently verified.
- **CONSOLE_LOGS (0x32):** reassemble `data[7:]` across frames, strip `34 00 01` markers →
  readable firmware text (`SUPERVISOR: SOC report: NN.NN` = battery %, `MAX77818_MANAGER`
  = charger IC, `IQS7223B`/`CAPSENSE` = wrist sensor, Nordic BLE stack).
- **COMMAND_RESPONSE (0x24):** version = skip 3 bytes then 8×u32 LE; battery = skip 3,
  then status + percent.

## Official API (the stable path for scored metrics)
- OAuth2 auth-code. Authorize `https://api.prod.whoop.com/oauth/oauth2/auth`,
  token `…/oauth/oauth2/token`, API base `https://api.prod.whoop.com/developer`.
- v2 endpoints: `/v2/recovery`, `/v2/activity/sleep`, `/v2/activity/workout`, `/v2/cycle`,
  `/v2/user/profile/basic`. Scores: SCORED / PENDING_SCORE / UNSCORABLE (don't show pending as 0).
- Scopes: `read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement offline`.
- Limits ~100/min, 10k/day.
