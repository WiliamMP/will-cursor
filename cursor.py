"""Move the mouse with your index finger via webcam. Pinch thumb + index to click/drag.

Run: .venv/Scripts/python cursor.py     (q or Esc in the preview window quits)
Test: .venv/Scripts/python cursor.py --test
"""
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request

# Tuning knobs: real cameras and hands differ, adjust here.
CAMERA = 0
MARGIN = 0.15        # ignore this fraction of each frame edge, so screen corners are reachable
SMOOTH_MIN = 0.08    # 0..1, how much the cursor follows small movements (lower = steadier, hits thin window edges)
SMOOTH_FAST = 200    # pixels of hand movement at which the cursor follows with no smoothing
PINCH_ON = 0.35      # thumb-index distance / hand size that starts a click
PINCH_OFF = 0.50     # distance that releases it (hysteresis stops flicker)
DRAG_START = 40      # pixels the hand must move while pinched before a click becomes a drag
DOUBLE_CLICK = 0.5   # seconds the cursor stays put after a click, so a second pinch double-clicks

# Clap (both hands meet in front of the camera) runs this command: Windows Terminal with Claude Code.
# -NoExit keeps the shell open after claude quits. For a site: ["cmd", "/c", "start", "", "https://..."]
CLAP_OPEN = ["wt.exe", "pwsh", "-NoExit", "-Command", "claude"]
DICTATE_AFTER = 4    # seconds after the clap to start voice typing (Win+H), once claude is up; 0 = off
CLAP_APART = 2.0    # palm distance, in hand sizes, that counts as hands open
CLAP_NEAR = 1.2      # hands this close and then merging into one blob also count as a clap
CLAP_TOGETHER = 0.7  # palm distance that counts as hands together
CLAP_TIME = 0.6      # seconds from open to together; slower is just moving hands, not a clap
CLAP_COOLDOWN = 1.5  # seconds before another clap can fire
FIST_HOLD = 0.4      # seconds to hold a closed fist before it presses Enter
REDICTATE = 0.5      # seconds after that Enter to turn voice typing (Win+H) back on; 0 = off

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")


def to_screen(x, y, w, h):
    """Camera point (0..1, already mirrored) to screen pixels, stretching the inner box to full screen."""
    def scale(v, size):
        v = (v - MARGIN) / (1 - 2 * MARGIN)
        return min(max(v, 0.0), 1.0) * (size - 1)
    return scale(x, w), scale(y, h)


def follow(cx, cy, tx, ty):
    """Move the cursor toward the target: heavy smoothing for small jitter, almost none for big moves."""
    a = min(1.0, SMOOTH_MIN + math.dist((cx, cy), (tx, ty)) / SMOOTH_FAST)
    return cx + (tx - cx) * a, cy + (ty - cy) * a


def pinch_state(ratio, pressed):
    """New pressed state for a thumb-index distance ratio, with hysteresis."""
    return ratio < PINCH_OFF if pressed else ratio < PINCH_ON


def hand_size(lm):
    """Wrist to middle knuckle, so distances work at any distance from the camera."""
    return math.dist((lm[0].x, lm[0].y), (lm[9].x, lm[9].y)) or 1e-6


def is_fist(lm):
    """All four fingertips curled in: each tip is closer to the wrist than its middle joint."""
    d = lambda i: math.dist((lm[i].x, lm[i].y), (lm[0].x, lm[0].y))
    return all(d(tip) < d(joint) for tip, joint in ((8, 6), (12, 10), (16, 14), (20, 18)))


class Clap:
    def __init__(self):
        self.apart_at = -9.0  # last time both hands were clearly apart
        self.near = False     # hands were close on the last two-hand frame
        self.fired = -9.0

    def feed(self, t, gap):
        """gap: palm distance / hand size when two hands are visible, else None. True on a clap."""
        if gap is None:
            # Hands that overlap often stop being detected as two: count that as the meeting.
            hit = self.near and t - self.apart_at < CLAP_TIME
            self.near = False
        else:
            if gap > CLAP_APART:
                self.apart_at = t
            self.near = gap < CLAP_NEAR
            hit = gap < CLAP_TOGETHER and t - self.apart_at < CLAP_TIME
        if hit and t - self.fired > CLAP_COOLDOWN:
            self.fired = t
            self.apart_at = -9.0  # the hands must open again before the next clap
            return True
        return False


def main():
    import cv2
    import mediapipe as mp
    import pyautogui
    from mediapipe.tasks.python import BaseOptions, vision

    if not os.path.exists(MODEL):
        print("Downloading hand model...")
        urllib.request.urlretrieve(MODEL_URL, MODEL)

    pyautogui.PAUSE = 0  # default 0.1s per call makes the cursor stutter
    screen_w, screen_h = pyautogui.size()
    landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=MODEL),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=2,
    ))
    cam = cv2.VideoCapture(CAMERA, cv2.CAP_MSMF)
    if not cam.isOpened():
        sys.exit(f"Could not open camera {CAMERA}")

    cx, cy = pyautogui.position()
    pressed = False
    press_at = (0, 0)
    hold_until = 0
    clapper = Clap()
    fist_since = None
    start = time.monotonic()
    try:
        while True:
            ok, frame = cam.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)  # mirror, so moving right moves the cursor right
            fh, fw = frame.shape[:2]
            image = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            result = landmarker.detect_for_video(image, int((time.monotonic() - start) * 1000))

            hands = result.hand_landmarks
            gap = None
            if len(hands) == 2:
                size = (hand_size(hands[0]) + hand_size(hands[1])) / 2
                gap = math.dist((hands[0][9].x, hands[0][9].y), (hands[1][9].x, hands[1][9].y)) / size
            if clapper.feed(time.monotonic(), gap):
                print(f"clap: running {' '.join(CLAP_OPEN)}")
                subprocess.Popen(CLAP_OPEN)
                if DICTATE_AFTER:
                    # Win+H is Windows' own voice typing: it types what you say into the focused window.
                    threading.Timer(DICTATE_AFTER, pyautogui.hotkey, ("win", "h")).start()
            for lm in hands:
                for p in lm:
                    cv2.circle(frame, (int(p.x * fw), int(p.y * fh)), 3, (240, 54, 91), -1)

            # Hold a closed fist = Enter (sends what voice typing wrote). Once per fist.
            fist = len(hands) == 1 and is_fist(hands[0])
            if not fist:
                fist_since = None
            elif fist_since is None:
                fist_since = time.monotonic()
            elif time.monotonic() - fist_since > FIST_HOLD:
                print("fist: Enter")
                pyautogui.press("enter")
                if REDICTATE:
                    # Any key press turns Windows voice typing off: switch it back on to keep talking.
                    threading.Timer(REDICTATE, pyautogui.hotkey, ("win", "h")).start()
                fist_since = math.inf  # sent; open the hand to arm it again

            # Two hands on screen = clapping, so the cursor waits; it only follows a single open hand.
            if len(hands) == 1 and not fist:
                lm = hands[0]
                tip, thumb, knuckle = lm[8], lm[4], lm[5]
                # Follow the index knuckle, not the tip: the tip moves when you pinch,
                # which turned every click into a small drag.
                tx, ty = to_screen(knuckle.x, knuckle.y, screen_w, screen_h)
                cx, cy = follow(cx, cy, tx, ty)

                ratio = math.dist((tip.x, tip.y), (thumb.x, thumb.y)) / hand_size(lm)
                now = pinch_state(ratio, pressed)
                if now and not pressed:
                    press_at = pyautogui.position()
                    pyautogui.mouseDown()
                elif pressed and not now:
                    pyautogui.mouseUp()
                    hold_until = time.monotonic() + DOUBLE_CLICK
                pressed = now
                # While pinched, and briefly after a click, hold the cursor still until the hand
                # clearly moves. Then a click stays a click, and a second pinch lands on the same
                # pixel, which Windows needs to count a double click.
                held = pressed or time.monotonic() < hold_until
                if not held or math.dist((cx, cy), press_at) > DRAG_START:
                    press_at = (-1e9, -1e9)  # hand moved away, stop holding
                    hold_until = 0
                    pyautogui.moveTo(cx, cy)

                cv2.circle(frame, (int(knuckle.x * fw), int(knuckle.y * fh)), 10,
                           (0, 255, 0) if pressed else (255, 255, 255), 2)
            elif pressed:  # hand lost (or second hand appeared) mid-drag: never leave the button stuck down
                pyautogui.mouseUp()
                pressed = False

            m = MARGIN
            cv2.rectangle(frame, (int(m * fw), int(m * fh)), (int((1 - m) * fw), int((1 - m) * fh)),
                          (120, 120, 120), 1)
            cv2.imshow("will-cursor (q to quit)", frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        if pressed:
            pyautogui.mouseUp()
        cam.release()
        cv2.destroyAllWindows()
        landmarker.close()


def test():
    assert to_screen(MARGIN, MARGIN, 1920, 1080) == (0, 0)
    assert to_screen(1 - MARGIN, 1 - MARGIN, 1920, 1080) == (1919, 1079)
    assert to_screen(0, 1, 1920, 1080) == (0, 1079)  # outside the box clamps to the edge
    assert follow(0, 0, 5, 0)[0] < 1              # jitter barely moves the cursor
    assert follow(0, 0, 400, 0) == (400, 0)       # a fast move follows at once
    assert pinch_state(PINCH_ON - 0.01, False)
    assert not pinch_state(PINCH_ON + 0.01, False)
    assert pinch_state(PINCH_ON + 0.01, True)       # hysteresis keeps it held
    assert not pinch_state(PINCH_OFF + 0.01, True)
    from types import SimpleNamespace as P
    open_hand = [P(x=0, y=0)] + [P(x=0, y=-i) for i in range(1, 21)]  # fingers stretched up
    fist = [P(x=0, y=-1)] * 21
    fist[0] = P(x=0, y=0)
    for joint in (6, 10, 14, 18):
        fist[joint] = P(x=0, y=-3)  # middle joints stick out, tips curl back near the palm
    assert not is_fist(open_hand) and is_fist(fist)
    c = Clap()
    assert not c.feed(0.0, 3.0)    # hands open
    assert c.feed(0.3, 0.5)        # meet fast: clap
    assert not c.feed(0.4, 0.5)    # still together: no repeat
    assert not c.feed(1.0, 3.0) and not c.feed(1.2, 0.5)  # cooldown
    c = Clap()
    c.feed(0.0, 3.0)
    assert not c.feed(2.0, 0.5)    # slow approach is not a clap
    c = Clap()
    c.feed(0.0, 3.0)
    c.feed(0.2, 1.0)
    assert c.feed(0.3, None)       # hands merged into one detection: clap
    print("ok")


if __name__ == "__main__":
    test() if "--test" in sys.argv else main()
