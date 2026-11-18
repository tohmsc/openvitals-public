#!/usr/bin/env python3
"""Characterize the 19200 stream, then try DTR/RTS reset to catch a boot banner."""
import sys, time, serial

PORT = "/dev/cu.usbserial-DP06AXHE"

def printable(b):
    return "".join(chr(c) if 32 <= c < 127 or c in (9,10,13) else "." for c in b)

def listen(baud, secs, label):
    ser = serial.Serial(PORT, baud, timeout=0.2)
    ser.reset_input_buffer()
    buf = bytearray(); t0 = time.time(); times = []
    while time.time() - t0 < secs:
        c = ser.read(4096)
        if c:
            times.append(time.time() - t0); buf += c
    ser.close()
    print(f"\n===== {label}: {baud} baud, {len(buf)} bytes in {secs}s =====")
    if buf:
        print("ASCII:", printable(bytes(buf))[:1500])
        print("HEX  :", buf[:600].hex(" "))
        if times:
            print(f"first byte @ {times[0]:.2f}s, last @ {times[-1]:.2f}s, bursts={len(times)}")
    else:
        print("(silent)")
    return bytes(buf)

# 1) Long passive listen at 19200
listen(19200, 20, "passive-19200")

# 2) DTR/RTS reset pulses at a few bauds to trigger a boot log
for baud in (115200, 19200, 9600):
    try:
        ser = serial.Serial(PORT, baud, timeout=0.2)
        # Pulse common reset wirings
        ser.dtr = False; ser.rts = True;  time.sleep(0.2)
        ser.dtr = True;  ser.rts = False; time.sleep(0.2)
        ser.dtr = False; ser.rts = False; time.sleep(0.1)
        ser.reset_input_buffer()
        buf = bytearray(); t0 = time.time()
        while time.time() - t0 < 4:
            c = ser.read(4096)
            if c: buf += c
        ser.close()
        print(f"\n--- after reset pulse @ {baud}: {len(buf)} bytes ---")
        if buf:
            print("ASCII:", printable(bytes(buf))[:1500])
            print("HEX  :", buf[:400].hex(" "))
    except Exception as e:
        print(f"[{baud}] {e}")
print("\ndone")
