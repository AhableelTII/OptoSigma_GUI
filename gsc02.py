"""Minimal driver for the Sigma Koki / OptoSigma GSC-02 controller + HPS60-20X stage.

Cross-platform (Windows COM ports and Linux /dev/tty*), using pyserial.
Protocol details that matter on this controller:
  - every move command carries an explicit sign ("A:1+P500", never "A:1P500");
  - the home command needs a direction ("H:1-");
  - DTR/RTS are raised, or some USB-serial adapters keep the GSC-02 silent;
  - Q: position fields are space-padded ("-     1000") and are parsed with the
    spaces removed, as the manufacturer's gsc02.py does.

THE DEFINITION OF ZERO:
    zero = home limit switch + ZERO_OFFSET_STEPS
The stage has no encoder: the controller only counts the steps it sends, so a
knob turned by hand, a power cycle or a stall shifts the real position without
changing the count. home_and_zero() re-finds the switch and rebuilds zero.
"""

import threading
import time

import serial
import serial.tools.list_ports

ZERO_OFFSET_STEPS = 10000      # home switch -> zero, ~10 mm (mid-travel of 20 mm)
MM_PER_STEP = 0.0009996        # HPS60-20X, measured with a dial indicator (nominal 1 um/step)
# Travel limits, from the stage's physical geometry: the home switch is
# ZERO_OFFSET_STEPS below zero and the HPS60-20X has 20 mm of travel, so the
# far end is about TRAVEL_STEPS - ZERO_OFFSET_STEPS above zero. EDGE_MARGIN
# keeps commands just clear of both ends; the controller's limit switches are
# the hardware backstop.
TRAVEL_STEPS = 20000           # 20 mm of travel
EDGE_MARGIN_STEPS = 100        # 0.1 mm
MIN_STEPS = -ZERO_OFFSET_STEPS + EDGE_MARGIN_STEPS                 # -9900, just above the home switch
MAX_STEPS = TRAVEL_STEPS - ZERO_OFFSET_STEPS - EDGE_MARGIN_STEPS   # +9900, just short of the far end


def in_travel(target):
    return MIN_STEPS <= target <= MAX_STEPS
AXIS = 1


class StageError(Exception):
    pass


def _is_usb_serial(p):
    text = f"{p.description} {p.manufacturer or ''} {p.hwid}".lower()
    return p.vid is not None or any(k in text for k in ("prolific", "pl2303", "usb-serial", "usb serial", "ftdi"))


def find_ports():
    """[(device, description, likely_stage)] for serial ports, USB-serial adapters first."""
    ports = sorted(serial.tools.list_ports.comports(), key=lambda p: not _is_usb_serial(p))
    return [(p.device, p.description, _is_usb_serial(p)) for p in ports]


class GSC02:
    def __init__(self, port, simulate=False):
        self._lock = threading.Lock()   # one command/response at a time (STOP can cut in between polls)
        if simulate:
            self._ser = FakeGSC02()
        else:
            try:
                self._ser = serial.Serial(port, 9600, bytesize=8, parity="N", stopbits=1,
                                          timeout=0.5, rtscts=False, dsrdtr=False, xonxoff=False)
            except serial.SerialException as e:
                raise StageError(f"Cannot open {port}: {e}") from e
            self._ser.dtr = True
            self._ser.rts = True
            time.sleep(0.1)
            self._ser.reset_input_buffer()
        if self.status2() not in ("R", "B"):
            self.close()
            raise StageError(f"No reply from a GSC-02 on {port} (check cable, power, port)")

    def close(self):
        try:
            self._ser.close()
        except Exception:
            pass

    # --- low level ---------------------------------------------------------
    def _send(self, cmd):
        self._ser.write((cmd + "\r\n").encode("ascii"))

    def _query(self, cmd):
        with self._lock:
            self._ser.reset_input_buffer()
            self._send(cmd)
            line = self._ser.readline().decode("ascii", "replace").strip()
        if not line:
            raise StageError(f"No reply to {cmd}")
        return line

    def _command(self, *cmds):
        with self._lock:
            for c in cmds:
                self._send(c)

    # --- status ------------------------------------------------------------
    def status2(self):
        """'R' ready, 'B' busy ("!:")."""
        try:
            return self._query("!:")[:1]
        except StageError:
            return ""

    def status(self):
        """(position_steps, ack2, ack3) from "Q:" -- ack2 'L'/'W' = axis 1 at a limit switch."""
        f = self._query("Q:").split(",")
        if len(f) < 5:
            raise StageError(f"Unexpected Q: reply: {','.join(f)}")
        return int(f[0].replace(" ", "")), f[3].strip()[:1], f[4].strip()[:1]

    def position(self):
        return self.status()[0]

    # --- motion ------------------------------------------------------------
    def move_relative(self, steps):
        sign = "-" if steps < 0 else "+"
        self._command(f"M:{AXIS}{sign}P{abs(int(steps))}", "G")

    def move_absolute(self, target):
        sign = "-" if target < 0 else "+"
        self._command(f"A:{AXIS}{sign}P{abs(int(target))}", "G")

    def home(self):
        self._command(f"H:{AXIS}-")

    def set_zero_here(self):
        self._command(f"R:{AXIS}")

    def stop(self, emergency=False):
        self._command("L:E" if emergency else f"L:{AXIS}")

    def wait_ready(self, timeout=60.0, on_position=None, poll=0.1, target=None):
        """Block until the stage reports Ready; calls on_position(steps) while it moves.

        Pass the target when known: a one-step move is over before the first
        poll ever sees Busy, and Ready-at-target ends the wait at once instead
        of after the 0.5 s fallback.
        """
        t0 = time.monotonic()
        seen_busy = False
        while True:
            pos, ack2, ack3 = self.status()
            if on_position:
                on_position(pos)
            if ack3 == "B":
                seen_busy = True
            # A very short move can finish before Busy is ever seen.
            elif ack3 == "R" and (seen_busy or pos == target or time.monotonic() - t0 > 0.5):
                return pos, ack2
            if time.monotonic() - t0 > timeout:
                raise StageError("Timed out waiting for the stage")
            time.sleep(poll)

    def home_and_zero(self, on_position=None):
        """Re-find the home switch, move out to the defined zero, set the count to 0."""
        self.home()
        self.wait_ready(timeout=90, on_position=on_position)
        self.move_relative(ZERO_OFFSET_STEPS)
        self.wait_ready(on_position=on_position)
        self.set_zero_here()
        time.sleep(0.05)
        if self.position() != 0:
            raise StageError("Re-zero did not end at 0")


class FakeGSC02:
    """Stand-in for the controller (--simulate): same replies and timing, no hardware.

    The carriage starts 1234 steps away from where the count says, as if moved
    by hand, so re-zeroing visibly fixes it.
    """
    SPEED = 4500.0   # steps/s, close to the measured average

    def __init__(self):
        self.count = 0          # what the controller reports
        self.real = 1234.0      # physical position relative to the true zero
        self.move = None        # (t_start, from_count, to_count, from_real, kind)
        self._rx = b""
        self._out = []

    def _update(self):
        if not self.move:
            return
        t0, c0, c1, r0, kind = self.move
        dur = abs(c1 - c0) / self.SPEED + 0.05
        frac = min(1.0, (time.monotonic() - t0) / dur)
        self.count = round(c0 + (c1 - c0) * frac)
        self.real = r0 + (c1 - c0) * frac
        if frac >= 1.0:
            if kind == "home":            # at the switch: real = -ZERO_OFFSET, count := 0
                self.count, self.real = 0, -ZERO_OFFSET_STEPS
            self.move = None

    def _start(self, target_count, kind="move"):
        self._update()
        self.move = (time.monotonic(), self.count, target_count, self.real, kind)

    def write(self, data):
        self._rx += data
        while b"\r\n" in self._rx:
            line, self._rx = self._rx.split(b"\r\n", 1)
            self._handle(line.decode())

    def _handle(self, cmd):
        self._update()
        busy = self.move is not None
        if cmd == "!:":
            self._out.append("B" if busy else "R")
        elif cmd == "Q:":
            self._out.append(f"{self.count:>10},{0:>10},K,M,{'B' if busy else 'R'}")
        elif busy:
            pass                                  # the real GSC-02 ignores moves while busy
        elif cmd.startswith("M:1"):
            self._pending = self.count + int(cmd[3] + cmd[5:])
        elif cmd.startswith("A:1"):
            self._pending = int(cmd[3] + cmd[5:])
        elif cmd == "G" and hasattr(self, "_pending"):
            self._start(self._pending)
            del self._pending
        elif cmd.startswith("H:1"):
            self._start(self.count - round(self.real + ZERO_OFFSET_STEPS), kind="home")
        elif cmd == "R:1":
            self.count = 0
        if cmd.startswith("L:") and self.move:
            self.move = None

    def readline(self):
        return (self._out.pop(0) + "\r\n").encode() if self._out else b""

    def reset_input_buffer(self):
        self._out.clear()

    def close(self):
        pass
