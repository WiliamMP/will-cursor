"""Clap twice to open something. Listens on the default microphone.

Run: .venv/Scripts/python clap.py     (Ctrl+C quits)
Test: .venv/Scripts/python clap.py --test
"""
import os
import sys
import time

# What to open: an .exe, a file, a folder or a URL (anything os.startfile accepts).
OPEN = "notepad.exe"

# Tuning knobs: every mic and room differs. Each detected clap prints its peak, use that to adjust.
CLAP_MIN = 0.25      # minimum peak (0..1) to count as a clap
CLAP_RATIO = 6       # the clap must be this many times louder than the background noise
GAP_MIN = 0.12       # seconds: ignore the echo/tail of the same clap
GAP_MAX = 0.7        # seconds: the second clap must come within this time of the first


class Claps:
    def __init__(self):
        self.noise = 0.01  # slow running average of block peaks
        self.last = -9.0   # time of the first clap of a pending pair

    def feed(self, t, peak):
        """Feed one audio block's peak at time t. Returns True when the second clap of a pair lands."""
        clap = peak > CLAP_MIN and peak > self.noise * CLAP_RATIO
        self.noise += (peak - self.noise) * 0.05
        if not clap or t - self.last < GAP_MIN:
            return False
        print(f"clap (peak {peak:.2f})")
        double = t - self.last <= GAP_MAX
        self.last = -9.0 if double else t
        return double


def main():
    import sounddevice as sd

    claps = Claps()

    def callback(data, frames, when, status):
        if claps.feed(time.monotonic(), float(abs(data).max())):
            print(f"double clap: opening {OPEN}")
            os.startfile(OPEN)

    print(f"Listening on {sd.query_devices(kind='input')['name']}. Clap twice to open {OPEN}.")
    with sd.InputStream(channels=1, samplerate=44100, blocksize=512, callback=callback):
        try:
            while True:
                sd.sleep(1000)
        except KeyboardInterrupt:
            pass


def test():
    c = Claps()
    assert not any(c.feed(i * 0.01, 0.02) for i in range(100))  # quiet room
    assert not c.feed(1.0, 0.8)    # first clap
    assert not c.feed(1.02, 0.6)   # its tail, ignored
    assert c.feed(1.4, 0.8)        # second clap: open
    assert not c.feed(3.0, 0.8)    # a lone clap later does not open
    assert not c.feed(4.0, 0.8)    # too late to pair with it
    c = Claps()
    for i in range(200):
        c.feed(i * 0.01, 0.3)      # loud music: background is high
    assert not c.feed(2.1, 0.5) and not c.feed(2.4, 0.5)
    print("ok")


if __name__ == "__main__":
    test() if "--test" in sys.argv else main()
