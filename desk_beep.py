#!/usr/bin/env python3
"""Beep like a reversing truck while the standing desk moves.

Reads the undocumented accelerometer on Apple Silicon MacBook Pros, so it needs root:
    sudo python3 desk_beep.py            # beep
    sudo python3 desk_beep.py --debug    # also print live levels, to tune --level
"""
import argparse
import ctypes
import math
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

vp = ctypes.c_void_p
cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

cf.CFStringCreateWithCString.restype = vp
cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
cf.CFNumberCreate.restype = vp
cf.CFNumberCreate.argtypes = [vp, ctypes.c_int, vp]
cf.CFNumberGetValue.restype = ctypes.c_bool
cf.CFNumberGetValue.argtypes = [vp, ctypes.c_int, vp]
cf.CFRunLoopGetCurrent.restype = vp
iokit.IOServiceMatching.restype = vp
iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
iokit.IOServiceGetMatchingServices.argtypes = [ctypes.c_uint32, vp, ctypes.POINTER(ctypes.c_uint32)]
iokit.IOIteratorNext.restype = ctypes.c_uint32
iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
iokit.IORegistryEntryCreateCFProperty.restype = vp
iokit.IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_uint32, vp, vp, ctypes.c_uint32]
iokit.IORegistryEntrySetCFProperty.argtypes = [ctypes.c_uint32, vp, vp]
iokit.IOHIDDeviceCreate.restype = vp
iokit.IOHIDDeviceCreate.argtypes = [vp, ctypes.c_uint32]
iokit.IOHIDDeviceOpen.argtypes = [vp, ctypes.c_uint32]
REPORT_CB = ctypes.CFUNCTYPE(None, vp, ctypes.c_int, vp, ctypes.c_int, ctypes.c_uint32,
                             ctypes.POINTER(ctypes.c_uint8), ctypes.c_long)
iokit.IOHIDDeviceRegisterInputReportCallback.argtypes = [vp, vp, ctypes.c_long, REPORT_CB, vp]
iokit.IOHIDDeviceScheduleWithRunLoop.argtypes = [vp, vp, vp]
RUN_LOOP_MODE = vp.in_dll(cf, "kCFRunLoopDefaultMode")
SINT32 = 3


def cfstr(s):
    return cf.CFStringCreateWithCString(None, s.encode(), 0x08000100)


def cfnum(n):
    return cf.CFNumberCreate(None, SINT32, ctypes.byref(ctypes.c_int32(n)))


def services(cls):
    it = ctypes.c_uint32()
    iokit.IOServiceGetMatchingServices(0, iokit.IOServiceMatching(cls), ctypes.byref(it))
    while True:
        s = iokit.IOIteratorNext(it)
        if not s:
            return
        yield s


def get_int(service, key):
    ref = iokit.IORegistryEntryCreateCFProperty(service, cfstr(key), None, 0)
    out = ctypes.c_int32(-1)
    if ref:
        cf.CFNumberGetValue(ref, SINT32, ctypes.byref(out))
    return out.value


def open_accelerometer(callback):
    """Wake the SPU sensors and stream accelerometer reports (x, y, z int32 at bytes 6..18, 1 g = 65536) into callback."""
    for drv in services(b"AppleSPUHIDDriver"):
        for key, val in (("SensorPropertyReportingState", 1), ("SensorPropertyPowerState", 1), ("ReportInterval", 1000)):
            iokit.IORegistryEntrySetCFProperty(drv, cfstr(key), cfnum(val))
    for svc in services(b"AppleSPUHIDDevice"):
        if get_int(svc, "PrimaryUsagePage") == 0xFF00 and get_int(svc, "PrimaryUsage") == 3:
            dev = iokit.IOHIDDeviceCreate(None, svc)
            rc = iokit.IOHIDDeviceOpen(dev, 0)
            if rc:
                sys.exit(f"Can't open the accelerometer (IOReturn 0x{rc & 0xFFFFFFFF:08x}). Run with sudo.")
            buf = (ctypes.c_uint8 * 4096)()
            iokit.IOHIDDeviceRegisterInputReportCallback(dev, buf, len(buf), callback, None)
            iokit.IOHIDDeviceScheduleWithRunLoop(dev, cf.CFRunLoopGetCurrent(), RUN_LOOP_MODE)
            return dev, buf
    sys.exit("No accelerometer found: this needs an Apple Silicon MacBook Pro.")


class Detector:
    """Flags sustained 15-35 Hz vibration along gravity: the desk motor hums at ~25 Hz, typing is much weaker there."""

    def __init__(self, fs, level, hold=0.2, band=(15, 35)):
        self.ag = 1 - math.exp(-1 / (fs * 2.0))   # gravity estimate, ~2 s
        self.alo, self.ahi = (1 - math.exp(-2 * math.pi * f / fs) for f in band)  # band-pass corners
        self.ae = 1 - math.exp(-1 / (fs * 0.25))  # vibration energy, ~250 ms
        self.hold = round(hold * fs)
        self.on, self.off = level, level / 2      # hysteresis, so the beep doesn't flicker mid-move
        self.g = None
        self.lo = self.bp = self.v2 = 0.0
        self.streak = 0
        self.moving = False

    @property
    def vertical(self):
        return math.sqrt(self.v2)

    def update(self, a):
        if self.g is None:
            self.g = list(a)
        g = self.g
        for i in range(3):
            g[i] += self.ag * (a[i] - g[i])
        d = [a[i] - g[i] for i in range(3)]
        v = sum(d[i] * g[i] for i in range(3)) / math.sqrt(sum(c * c for c in g))
        self.lo += self.alo * (v - self.lo)             # v - lo: high-pass
        self.bp += self.ahi * (v - self.lo - self.bp)   # then low-pass
        self.v2 += self.ae * (self.bp * self.bp - self.v2)

        want = self.vertical > (self.off if self.moving else self.on)
        if want != self.moving:
            self.streak += 1
            if self.streak >= self.hold:
                self.moving, self.streak = want, 0
        else:
            self.streak = 0
        return self.moving


def make_beep(path, freq=1100, rate=44100):
    """One second of reversing alarm: 0.5 s buzzer tone, 0.5 s silence."""
    on, fade = rate // 2, rate // 200
    frames = bytearray()
    for i in range(rate):
        val = 0
        if i < on:
            t = i / rate
            tone = sum(math.sin(2 * math.pi * freq * k * t) / k for k in (1, 3, 5))
            val = int(12000 * min(1.0, i / fade, (on - i) / fade) * tone)
        frames += struct.pack("<h", val)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def beeper(moving, path):
    while True:
        moving.wait()
        subprocess.run(["afplay", path])


def main():
    p = argparse.ArgumentParser(description="Reversing-truck beep while the standing desk moves.")
    p.add_argument("--level", type=float, default=0.003, help="vertical 15-35 Hz vibration (g RMS) that counts as moving")
    p.add_argument("--debug", action="store_true", help="print live vertical level")
    args = p.parse_args()

    # Ctrl-C must kill us even while CoreFoundation is inside our callback (ctypes swallows exceptions there).
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    path = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    make_beep(path)
    moving = threading.Event()
    threading.Thread(target=beeper, args=(moving, path), daemon=True).start()

    det = None
    count, t0, last_print = 0, time.monotonic(), 0.0

    @REPORT_CB
    def on_report(ctx, result, sender, rtype, rid, report, length):
        nonlocal det, count, last_print
        if length != 22:
            return
        a = [c / 65536 for c in struct.unpack_from("<iii", ctypes.string_at(report, length), 6)]
        now = time.monotonic()
        if det is None:  # measure the sample rate for a second before detecting
            count += 1
            if now - t0 >= 1.0:
                det = Detector(count / (now - t0), args.level)
                print(f"Listening at {count / (now - t0):.0f} Hz. Move the desk!")
            return
        if det.update(a):
            moving.set()
        else:
            moving.clear()
        if args.debug and now - last_print >= 0.2:
            last_print = now
            print(f"vertical {det.vertical:.4f} g  {'BEEP' if det.moving else ''}")

    keep = open_accelerometer(on_report)  # noqa: F841 - keeps the device and report buffer alive
    print("Waiting for the accelerometer... (if this hangs, run with sudo)")
    cf.CFRunLoopRun()


if __name__ == "__main__":
    main()
