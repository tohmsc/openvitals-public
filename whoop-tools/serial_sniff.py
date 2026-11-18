#!/usr/bin/env python3
"""Sweep common baud rates on the FTDI UART and log any output (raw + hex).

Usage: serial_sniff.py [PORT] [SECONDS_PER_BAUD]
Reads passively. The device is already powered on the charger, so we may only
see periodic chatter; a banner/boot log usually needs a device reset.
"""
import sys, time, serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/cu.usbserial-DP06AXHE"
DWELL = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
BAUDS = [115200, 9600, 57600, 38400, 19200, 230400, 460800, 921600]

def printable(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 or c in (9, 10, 13) else "." for c in b)

for baud in BAUDS:
    try:
        ser = serial.Serial(PORT, baud, timeout=0.3)
    except Exception as e:
        print(f"[{baud}] open failed: {e}")
        continue
    ser.reset_input_buffer()
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < DWELL:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
    ser.close()
    if buf:
        print(f"\n===== {baud} baud: {len(buf)} bytes =====")
        print("ASCII:", printable(bytes(buf))[:2000])
        print("HEX  :", buf[:400].hex(" "))
    else:
        print(f"[{baud}] silent")
print("\ndone")
