"""Double-tap Ctrl detection.

DoubleCtrlDetector is pure logic (no OS calls) so it can be unit-tested.
start_listener() wires it to a global keyboard hook via pynput.
"""
import time


class DoubleCtrlDetector:
    """Fires when Ctrl is tapped twice quickly, with no other key in between.

    A 'tap' = Ctrl pressed and released within max_hold seconds, without any other
    key being pressed while it was down (so Ctrl+C, Ctrl+Click etc. never count).
    """

    def __init__(self, on_trigger, gap_ms=400, max_hold_ms=350, clock=time.monotonic):
        self.on_trigger = on_trigger
        self.gap = gap_ms / 1000.0
        self.max_hold = max_hold_ms / 1000.0
        self.clock = clock
        self._down_at = None        # time the current Ctrl press started
        self._chord = False         # another key was used while Ctrl was down
        self._last_tap = None

    def set_gap_ms(self, gap_ms):
        self.gap = gap_ms / 1000.0

    def ctrl_down(self):
        if self._down_at is None:   # ignore auto-repeat events while held
            self._down_at = self.clock()
            self._chord = False

    def ctrl_up(self):
        if self._down_at is None:
            return
        now = self.clock()
        held = now - self._down_at
        clean = (not self._chord) and held <= self.max_hold
        self._down_at = None
        if not clean:
            self._last_tap = None
            return
        if self._last_tap is not None and now - self._last_tap <= self.gap:
            self._last_tap = None
            self.on_trigger()
        else:
            self._last_tap = now

    def other_key(self):
        """Any non-Ctrl key or mouse button went down."""
        self._chord = True
        self._last_tap = None


def start_listener(detector: DoubleCtrlDetector):
    """Start the global hook. Returns the pynput listener (call .stop() to end)."""
    from pynput import keyboard, mouse

    ctrl_keys = {keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r}

    def on_press(key):
        if key in ctrl_keys:
            detector.ctrl_down()
        else:
            detector.other_key()

    def on_release(key):
        if key in ctrl_keys:
            detector.ctrl_up()

    def on_click(x, y, button, pressed):
        if pressed:
            detector.other_key()      # Ctrl+click should not count as a tap

    klistener = keyboard.Listener(on_press=on_press, on_release=on_release)
    klistener.daemon = True
    klistener.start()
    mlistener = mouse.Listener(on_click=on_click)
    mlistener.daemon = True
    mlistener.start()
    return klistener, mlistener
