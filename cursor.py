"""Move the mouse with your index finger via webcam. Pinch thumb + index to click/drag.

Gestures (one hand): pinch thumb + middle = right click; index + middle up, others curled = scroll
by moving the hand up/down; hold a fist still = Enter; sweep an open palm sideways = switch virtual
desktop; sweep a fist sideways = throw the window to the next monitor.
Two hands: clap = open Claude Code; pinch both and spread/close them = zoom; two fists held = pause/resume
(paused, only the two fists work).

Run: .venv/Scripts/python cursor.py     (Ctrl+C in the terminal quits; q or Esc in the preview if HUD is off)
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
MARGIN = 0.25        # ignore this fraction of each frame edge, so screen corners are reachable (higher = less reach, less precision)
SMOOTH_MIN = 0.08    # 0..1, how much the cursor follows small movements (lower = steadier, hits thin window edges)
SMOOTH_FAST = 200    # pixels of hand movement at which the cursor follows with no smoothing
PINCH_ON = 0.35      # thumb-index distance / hand size that starts a click
PINCH_OFF = 0.50     # distance that releases it (hysteresis stops flicker)
DRAG_START = 40      # pixels the hand must move while pinched before a click becomes a drag
DOUBLE_CLICK = 0.5   # seconds the cursor stays put after a click, so a second pinch double-clicks
SCROLL_SPEED = 3000  # wheel units (120 = one notch) per full frame height of hand movement; negative flips it
SWIPE_DIST = 0.35    # fraction of the frame width a swipe must sweep sideways...
SWIPE_TIME = 0.25    # ...within this many seconds; slower is just moving the cursor
SWIPE_COOLDOWN = 0.8 # seconds before another swipe can fire
ZOOM_STEP = 0.4      # change in distance between the pinched hands, in hand sizes, per zoom notch
SOUNDS = True        # beep on clicks and gestures
HUD = True           # draw the hand over the whole screen (click-through) instead of the camera preview window

# Clap (both hands meet in front of the camera) runs this command: Windows Terminal with Claude Code.
# -NoExit keeps the shell open after claude quits. For a site: ["cmd", "/c", "start", "", "https://..."]
CLAP_OPEN = ["wt.exe", "pwsh", "-NoExit", "-Command", "claude"]
DICTATE_AFTER = 4    # seconds after the new terminal takes focus to start voice typing (Win+H), so claude is up; 0 = off
FOCUS_TIMEOUT = 15   # seconds to wait for the new terminal to take focus before giving up on voice typing
CLAP_APART = 2.0    # palm distance, in hand sizes, that counts as hands open
CLAP_NEAR = 1.2      # hands this close and then merging into one blob also count as a clap
CLAP_TOGETHER = 0.7  # palm distance that counts as hands together
CLAP_TIME = 0.6      # seconds from open to together; slower is just moving hands, not a clap
CLAP_COOLDOWN = 1.5  # seconds before another clap can fire
FIST_HOLD = 0.4      # seconds to hold a fist (Enter) or two fists (pause) before it fires
STILL_SPEED = 0.5    # frame widths per second under which a fist counts as held still; faster is a throw
REDICTATE = 0.5      # seconds after that Enter to turn voice typing (Win+H) back on; 0 = off
# Fist Enter only fires in these windows (Windows Terminal, classic console), so a stray fist can't submit a form.
TERMINAL_CLASSES = ("CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass")

# HUD / preview color per state.
COLORS = {"": "#36d6ff", "CLICK": "#3dff8b", "RIGHT": "#ff4d6d", "SCROLL": "#ffc93c",
          "ZOOM": "#c77dff", "PAUSED": "#ff4d6d"}

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_landmarker.task")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/latest/hand_landmarker.task")


def to_screen(x, y, w, h):
    """Camera point (0..1, already mirrored) to screen pixels, stretching the inner box to full screen.
    Stays 1px off the edges: pyautogui's fail-safe aborts once the cursor sits in a screen corner."""
    def scale(v, size):
        v = (v - MARGIN) / (1 - 2 * MARGIN)
        return 1 + min(max(v, 0.0), 1.0) * (size - 3)
    return scale(x, w), scale(y, h)


def follow(cx, cy, tx, ty):
    """Move the cursor toward the target: heavy smoothing for small jitter, almost none for big moves."""
    a = min(1.0, SMOOTH_MIN + math.dist((cx, cy), (tx, ty)) / SMOOTH_FAST)
    return cx + (tx - cx) * a, cy + (ty - cy) * a


def pinch_state(ratio, pressed):
    """New pressed state for a thumb-finger distance ratio, with hysteresis."""
    return ratio < PINCH_OFF if pressed else ratio < PINCH_ON


def hand_size(lm):
    """Wrist to middle knuckle, so distances work at any distance from the camera."""
    return math.dist((lm[0].x, lm[0].y), (lm[9].x, lm[9].y)) or 1e-6


def pinch_ratio(lm, finger=8):
    """Thumb tip to a fingertip, in hand sizes."""
    return math.dist((lm[finger].x, lm[finger].y), (lm[4].x, lm[4].y)) / hand_size(lm)


def extended(lm, tip, joint):
    """Finger stretched out: its tip is farther from the wrist than its middle joint."""
    d = lambda i: math.dist((lm[i].x, lm[i].y), (lm[0].x, lm[0].y))
    return d(tip) > d(joint)


FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))  # (tip, middle joint) of index, middle, ring, pinky


def is_fist(lm):
    """All four fingertips curled in."""
    return not any(extended(lm, tip, joint) for tip, joint in FINGERS)


def is_palm(lm):
    """All four fingers stretched out."""
    return all(extended(lm, tip, joint) for tip, joint in FINGERS)


def is_scroll(lm):
    """Index and middle up, ring and pinky curled."""
    return (extended(lm, 8, 6) and extended(lm, 12, 10)
            and not extended(lm, 16, 14) and not extended(lm, 20, 18))


def beep(*notes):
    """Play (frequency Hz, milliseconds) notes without blocking the camera loop."""
    if SOUNDS:
        import winsound
        threading.Thread(target=lambda: [winsound.Beep(f, ms) for f, ms in notes], daemon=True).start()


def foreground():
    """(handle, window class) of the focused window."""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return hwnd, buf.value


def dictate_after_clap(before):
    """Wait for the terminal the clap opened to take focus, give claude time to start, then Win+H.
    Win+H is Windows' own voice typing: it types what you say into the focused window."""
    import pyautogui
    end = time.monotonic() + FOCUS_TIMEOUT
    while True:
        hwnd, cls = foreground()
        if hwnd != before and cls in TERMINAL_CLASSES:
            break
        if time.monotonic() > end:
            print("clap: terminal never took focus, no voice typing")
            return
        time.sleep(0.2)
    time.sleep(DICTATE_AFTER)
    if foreground()[0] == hwnd:  # user may have switched away while claude started
        pyautogui.hotkey("win", "h")


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


class Hold:
    """Fires a gesture once after it is held still for FIST_HOLD. Open the hands to arm it again, so
    dropping one fist of a pause, or a fist coming to rest after a throw, does not turn into an Enter."""
    def __init__(self):
        self.gesture, self.since, self.armed = None, 0.0, True

    def feed(self, t, gesture, still=True):
        if gesture is None:
            self.armed = True
        if gesture != self.gesture or not still:
            self.gesture, self.since = gesture, t
        elif gesture and self.armed and t - self.since > FIST_HOLD:
            self.armed = False
            return gesture
        return None


class Swipe:
    """Fast sideways sweep of one hand. feed() returns 1 (right), -1 (left) or 0."""
    def __init__(self):
        self.trail = []  # (t, x, y) over the last SWIPE_TIME
        self.fired = -9.0

    def feed(self, t, x, y):
        self.trail = [p for p in self.trail if t - p[0] <= SWIPE_TIME] + [(t, x, y)]
        dx, dy = x - self.trail[0][1], y - self.trail[0][2]
        if abs(dx) > SWIPE_DIST and abs(dy) < abs(dx) / 2 and t - self.fired > SWIPE_COOLDOWN:
            self.fired, self.trail = t, []
            return 1 if dx > 0 else -1
        return 0


class Hud:
    """Always-on-top, click-through overlay that draws the hand over the whole screen."""
    BONES = ((0, 1, 2, 3, 4), (0, 5, 6, 7, 8), (5, 9, 10, 11, 12), (9, 13, 14, 15, 16),
             (13, 17, 18, 19, 20), (0, 17))

    def __init__(self, w, h):
        import ctypes
        import tkinter
        self.w, self.h = w, h
        self.root = tkinter.Tk()
        self.root.overrideredirect(True)
        self.root.geometry(f"{w}x{h}+0+0")
        self.root.attributes("-topmost", True, "-transparentcolor", "black")
        self.canvas = tkinter.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.root.update()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(self.root.winfo_id())
        # Layered + transparent: clicks fall through to the windows below.
        # Tool window + no-activate: no taskbar button, never steals focus.
        user32.SetWindowLongW(hwnd, -20, user32.GetWindowLongW(hwnd, -20) | 0x80000 | 0x20 | 0x80 | 0x8000000)

    def draw(self, hands, cursor, text, color):
        c = self.canvas
        c.delete("all")
        for lm in hands:
            # Same mapping as the cursor, unclamped, so the drawn hand sits on the cursor.
            pts = [((p.x - MARGIN) / (1 - 2 * MARGIN) * self.w, (p.y - MARGIN) / (1 - 2 * MARGIN) * self.h)
                   for p in lm]
            for bone in self.BONES:
                c.create_line(*[v for i in bone for v in pts[i]], fill=color, width=2)
            for x, y in pts:
                c.create_oval(x - 4, y - 4, x + 4, y + 4, outline=color, width=2)
        if cursor:
            x, y = cursor
            c.create_oval(x - 18, y - 18, x + 18, y + 18, outline=color, width=3)
        c.create_text(30, 30, anchor="nw", text=text, fill=color, font=("Consolas", 18, "bold"))
        self.root.update()

    def close(self):
        self.root.destroy()


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
    pressed = right = zooming = paused = False
    press_at = (0, 0)
    hold_until = 0
    scroll_y = zoom_gap = None
    scroll_rest = zoom_rest = 0.0
    last_knuckle = None  # (t, x, y) of the single hand's index knuckle on the previous frame
    swipe_pose = None
    notice, notice_until = "", 0.0
    clapper, holder, swiper = Clap(), Hold(), Swipe()
    hud = Hud(screen_w, screen_h) if HUD else None
    start = time.monotonic()

    def note(msg):
        """Print an event and flash it on the HUD / preview."""
        nonlocal notice, notice_until
        print(msg)
        notice, notice_until = msg, time.monotonic() + 1.5

    print("Running. Ctrl+C here quits." if HUD else "Running. q or Esc in the preview quits.")
    try:
        while True:
            ok, frame = cam.read()
            if not ok:
                break
            frame = cv2.flip(frame, 1)  # mirror, so moving right moves the cursor right
            image = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            t = time.monotonic()
            result = landmarker.detect_for_video(image, int((t - start) * 1000))

            hands = result.hand_landmarks
            gap = None
            if len(hands) == 2:
                size = (hand_size(hands[0]) + hand_size(hands[1])) / 2
                gap = math.dist((hands[0][9].x, hands[0][9].y), (hands[1][9].x, hands[1][9].y)) / size

            # Both hands pinched = zoom (Ctrl+wheel): spread them to zoom in, bring them together to zoom out.
            zoom_now = (gap is not None and not paused
                        and all(pinch_state(pinch_ratio(lm), zooming) for lm in hands))
            if zoom_now and zoom_gap is not None:
                zoom_rest += (gap - zoom_gap) / ZOOM_STEP
                if abs(zoom_rest) >= 1:
                    steps = int(zoom_rest)
                    zoom_rest -= steps
                    pyautogui.keyDown("ctrl")
                    pyautogui.scroll(120 * steps)
                    pyautogui.keyUp("ctrl")
                    beep((2000 if steps > 0 else 1500, 20))
            if not zoom_now:
                zoom_rest = 0.0
            zoom_gap = gap if zoom_now else None
            zooming = zoom_now

            if clapper.feed(t, gap) and not paused and not zooming:
                note("CLAP: opening Claude Code")
                beep((523, 60), (659, 60), (784, 60), (1047, 140))
                before = foreground()[0]
                subprocess.Popen(CLAP_OPEN)
                if DICTATE_AFTER:
                    threading.Thread(target=dictate_after_clap, args=(before,), daemon=True).start()

            # Sideways sweep: open palm switches virtual desktop, fist throws the window to the next monitor.
            pose = None
            if len(hands) == 1 and not paused and not pressed:
                pose = "fist" if is_fist(hands[0]) else "palm" if is_palm(hands[0]) else None
            if pose != swipe_pose:
                swiper, swipe_pose = Swipe(), pose  # a sweep must stay in one pose from start to end
            if pose and (way := swiper.feed(t, hands[0][5].x, hands[0][5].y)):
                side = "right" if way > 0 else "left"
                holder.armed = False  # the fist comes to rest after a throw: that is not an Enter
                if pose == "palm":
                    pyautogui.hotkey("win", "ctrl", side)
                    note(f"SWIPE: desktop {side}")
                else:
                    pyautogui.hotkey("win", "shift", side)
                    note(f"THROW: window {side}")
                beep((700, 30), (1100, 30)) if way > 0 else beep((1100, 30), (700, 30))

            # One fist held still = Enter (sends what voice typing wrote), two fists = pause/resume.
            speed = 0.0  # single hand speed, in frame widths per second
            if len(hands) == 1:
                k = hands[0][5]
                if last_knuckle:
                    speed = math.dist((k.x, k.y), last_knuckle[1:]) / max(t - last_knuckle[0], 1e-3)
                last_knuckle = (t, k.x, k.y)
            else:
                last_knuckle = None
            fists = sum(is_fist(lm) for lm in hands)
            gesture = "pause" if fists == 2 else "enter" if len(hands) == 1 and fists == 1 else None
            fired = holder.feed(t, gesture, still=speed < STILL_SPEED)
            if fired == "pause":
                paused = not paused
                note("PAUSED" if paused else "RESUMED")
                beep((900, 60), (600, 90)) if paused else beep((600, 60), (900, 90))
            elif fired == "enter" and not paused:
                if foreground()[1] in TERMINAL_CLASSES:
                    note("ENTER")
                    beep((1000, 40))
                    pyautogui.press("enter")
                    if REDICTATE:
                        # Any key press turns Windows voice typing off: switch it back on to keep talking.
                        threading.Timer(REDICTATE, pyautogui.hotkey, ("win", "h")).start()
                else:
                    note("ENTER ignored: not a terminal")

            # The cursor only follows a single open hand: two hands are clapping/zooming, a fist is a command.
            mode = None
            if len(hands) == 1 and not paused and not gesture:
                lm = hands[0]
                mode = "scroll" if is_scroll(lm) else "point"
            if mode != "point":
                if pressed:  # hand lost, paused or scrolling mid-drag: never leave the button stuck down
                    pyautogui.mouseUp()
                pressed = right = False
            if mode != "scroll":
                scroll_y = None

            if mode == "scroll":
                knuckle = lm[5]
                if scroll_y is not None:
                    scroll_rest += (scroll_y - knuckle.y) * SCROLL_SPEED  # hand up = scroll up
                    if abs(scroll_rest) >= 1:
                        pyautogui.scroll(int(scroll_rest))
                        scroll_rest -= int(scroll_rest)
                scroll_y = knuckle.y
            elif mode == "point":
                knuckle = lm[5]
                # Follow the index knuckle, not the tip: the tip moves when you pinch,
                # which turned every click into a small drag.
                tx, ty = to_screen(knuckle.x, knuckle.y, screen_w, screen_h)
                cx, cy = follow(cx, cy, tx, ty)

                ratio, mid = pinch_ratio(lm, 8), pinch_ratio(lm, 12)
                # Whichever finger is closer to the thumb wins, so one pinch never fires both buttons.
                now = pinch_state(ratio, pressed) and ratio <= mid
                right_now = pinch_state(mid, right) and mid < ratio
                if right_now and not right:
                    beep((1400, 20), (1800, 20))
                    pyautogui.rightClick()
                right = right_now
                if now and not pressed:
                    beep((1800, 20))
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

            status = ("PAUSED" if paused else "ZOOM" if zooming else "SCROLL" if mode == "scroll"
                      else "CLICK" if pressed else "RIGHT" if right else "")
            color = COLORS[status]
            text = notice if t < notice_until else status
            if hud:
                hud.draw(hands, (cx, cy) if mode == "point" else None, text, color)
            else:
                fh, fw = frame.shape[:2]
                bgr = tuple(int(color[i:i + 2], 16) for i in (5, 3, 1))
                for lm in hands:
                    for p in lm:
                        cv2.circle(frame, (int(p.x * fw), int(p.y * fh)), 3, bgr, -1)
                m = MARGIN
                cv2.rectangle(frame, (int(m * fw), int(m * fh)), (int((1 - m) * fw), int((1 - m) * fh)),
                              (120, 120, 120), 1)
                cv2.putText(frame, text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, bgr, 2)
                cv2.imshow("will-cursor (q to quit)", frame)
                if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if pressed:
            pyautogui.mouseUp()
        cam.release()
        if hud:
            hud.close()
        else:
            cv2.destroyAllWindows()
        landmarker.close()


def test():
    assert to_screen(MARGIN, MARGIN, 1920, 1080) == (1, 1)  # never (0, 0): fail-safe corner
    assert to_screen(1 - MARGIN, 1 - MARGIN, 1920, 1080) == (1918, 1078)
    assert to_screen(0, 1, 1920, 1080) == (1, 1078)  # outside the box clamps to the edge
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
    assert is_palm(open_hand) and not is_palm(fist)
    peace = list(fist)
    peace[8], peace[12] = P(x=0, y=-5), P(x=0, y=-5)  # index and middle back up
    assert is_scroll(peace) and not is_scroll(open_hand) and not is_scroll(fist) and not is_fist(peace)
    assert not is_palm(peace)
    h = Hold()
    assert h.feed(0.0, "pause") is None
    assert h.feed(0.5, "pause") == "pause"
    assert h.feed(0.6, "pause") is None       # once per hold
    assert h.feed(0.7, "enter") is None and h.feed(1.5, "enter") is None  # dropping one fist: no Enter
    assert h.feed(1.6, None) is None and h.feed(1.7, "enter") is None
    assert h.feed(2.2, "enter") == "enter"    # after opening the hands it works again
    h = Hold()
    h.feed(0.0, "enter")
    assert h.feed(0.3, "enter", still=False) is None  # a moving fist restarts the timer
    assert h.feed(0.6, "enter") is None and h.feed(0.8, "enter") == "enter"
    s = Swipe()
    assert not s.feed(0.0, 0.2, 0.5) and not s.feed(0.1, 0.4, 0.5)
    assert s.feed(0.2, 0.6, 0.5) == 1          # 0.4 of the frame in 0.2s: swipe right
    assert not s.feed(0.3, 0.2, 0.5) and not s.feed(0.4, 0.0, 0.5)  # cooldown
    assert s.feed(1.2, 0.0, 0.5) == 0 and s.feed(1.4, -0.4, 0.5) == -1
    s = Swipe()
    s.feed(0.0, 0.2, 0.5)
    assert not s.feed(1.0, 0.8, 0.5)           # same distance, slowly: just moving the cursor
    s = Swipe()
    s.feed(0.0, 0.2, 0.2)
    assert not s.feed(0.2, 0.6, 0.8)           # mostly diagonal: not a swipe
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
