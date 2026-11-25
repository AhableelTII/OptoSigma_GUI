"""Stage Control -- minimal GUI for the GSC-02 + HPS60-20X focus stage.

Runs on Windows and Linux (Python 3 + pyserial; Tkinter is built in).

    python stage_gui.py                # auto-detects the stage's serial port
    python stage_gui.py --port COM4    # or /dev/ttyUSB0
    python stage_gui.py --simulate     # no hardware: a simulated controller

On start-up it connects and RE-ZEROES AUTOMATICALLY: the stage drives to its
home limit switch and back out to the defined zero (home + 10,000 steps), so
positions are true even if the stage was moved by hand or power-cycled.
The stage moves as soon as the window opens.

Controls: step size (steps or mm) with Backward/Forward, single +/-1 step,
Go to zero, Re-zero, Max scan (positive end -> negative end -> zero), and STOP. Targets outside the stage's travel (-9900 to
+9900 steps from zero, 0.1 mm clear of each end) are refused.
"""

import argparse
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import gsc02
from gsc02 import GSC02, MAX_STEPS, MIN_STEPS, MM_PER_STEP, StageError, in_travel


class StageApp:
    def __init__(self, root, port=None, simulate=False):
        self.root = root
        self.simulate = simulate
        self.stage = None
        self.jobs = queue.Queue()          # motion jobs, run one at a time on a worker thread
        self.ui = queue.Queue()            # updates from the worker, applied on the Tk thread
        self.busy = False
        self.position = None
        self.stop_requested = False        # set by STOP; ends a multi-move job like Max scan

        root.title("Stage Control" + (" (SIMULATED)" if simulate else ""))
        root.resizable(False, False)
        self._build(port)
        threading.Thread(target=self._worker, daemon=True).start()
        root.after(50, self._drain_ui)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Connect and re-zero automatically on start-up.
        root.after(300, self.connect)

    # ------------------------------------------------------------------ layout
    def _build(self, port):
        pad = dict(padx=6, pady=4)
        main = ttk.Frame(self.root, padding=10)
        main.grid()

        conn = ttk.LabelFrame(main, text="Connection", padding=6)
        conn.grid(row=0, column=0, sticky="ew", **pad)
        found = [] if self.simulate else gsc02.find_ports()
        ports = ["(simulated)"] if self.simulate else [d for d, _, _ in found]
        # Auto-connect only to a USB-serial adapter, never to a built-in COM1 / ttyS0.
        likely = ["(simulated)"] if self.simulate else [d for d, _, usb in found if usb]
        self.port_var = tk.StringVar(value=port or (likely[0] if likely else ""))
        self.port_box = ttk.Combobox(conn, textvariable=self.port_var, values=ports, width=18)
        self.port_box.grid(row=0, column=0, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect + re-zero", command=self.connect)
        self.connect_btn.grid(row=0, column=1, **pad)
        ttk.Button(conn, text="Refresh ports", command=self._refresh_ports).grid(row=0, column=2, **pad)

        pos = ttk.LabelFrame(main, text="Position (from zero)", padding=6)
        pos.grid(row=1, column=0, sticky="ew", **pad)
        self.pos_steps = ttk.Label(pos, text="---", font=("TkDefaultFont", 26, "bold"), width=11, anchor="e")
        self.pos_steps.grid(row=0, column=0)
        ttk.Label(pos, text="steps", font=("TkDefaultFont", 12)).grid(row=0, column=1, sticky="w")
        self.pos_mm = ttk.Label(pos, text="", font=("TkDefaultFont", 14), width=11, anchor="e")
        self.pos_mm.grid(row=1, column=0)
        ttk.Label(pos, text="mm", font=("TkDefaultFont", 12)).grid(row=1, column=1, sticky="w")
        self.state_lbl = ttk.Label(pos, text="Not connected", font=("TkDefaultFont", 11, "bold"))
        self.state_lbl.grid(row=0, column=2, rowspan=2, padx=16)

        move = ttk.LabelFrame(main, text="Move", padding=6)
        move.grid(row=2, column=0, sticky="ew", **pad)
        ttk.Label(move, text="Step size:").grid(row=0, column=0, sticky="e", **pad)
        self.size_var = tk.StringVar(value="100")
        ttk.Entry(move, textvariable=self.size_var, width=10, justify="right").grid(row=0, column=1, **pad)
        self.unit_var = tk.StringVar(value="steps")
        ttk.Combobox(move, textvariable=self.unit_var, values=["steps", "mm"], width=6,
                     state="readonly").grid(row=0, column=2, **pad)
        presets = ttk.Frame(move)
        presets.grid(row=0, column=3, columnspan=2, sticky="w")
        for n in (1, 10, 100, 1000):
            ttk.Button(presets, text=str(n), width=5,
                       command=lambda n=n: (self.size_var.set(str(n)), self.unit_var.set("steps"))).pack(side="left", padx=1)

        self.motion_buttons = []
        def mbtn(text, cmd, row, col, colspan=1, width=16):
            b = ttk.Button(move, text=text, command=cmd, width=width)
            b.grid(row=row, column=col, columnspan=colspan, sticky="ew", **pad)
            self.motion_buttons.append(b)
            return b
        mbtn("<< Backward  (-)", lambda: self.jog(-1), 1, 0, 2)
        mbtn("Forward  (+) >>", lambda: self.jog(+1), 1, 2, 3)
        mbtn("-1 step", lambda: self.move_by(-1), 2, 0, 2)
        mbtn("+1 step", lambda: self.move_by(+1), 2, 2, 3)
        mbtn("Go to zero", self.go_zero, 3, 0, 2)
        mbtn("Re-zero (home switch)", self.rezero, 3, 2, 3)
        mbtn(f"Max scan:  {MAX_STEPS:+d}  ->  {MIN_STEPS:+d}  ->  0", self.max_scan, 4, 0, 5)
        stop = tk.Button(move, text="STOP", command=self.stop, bg="#c62828", fg="white",
                         activebackground="#8e0000", activeforeground="white",
                         font=("TkDefaultFont", 12, "bold"))
        stop.grid(row=5, column=0, columnspan=5, sticky="ew", **pad)
        ttk.Label(move, text=f"Negative (-) is toward the home switch. Travel: {MIN_STEPS:+d} to "
                             f"{MAX_STEPS:+d} steps ({MIN_STEPS * MM_PER_STEP:+.2f} to {MAX_STEPS * MM_PER_STEP:+.2f} mm).",
                  foreground="gray30").grid(row=6, column=0, columnspan=5, sticky="w", **pad)

        logf = ttk.LabelFrame(main, text="Log", padding=6)
        logf.grid(row=3, column=0, sticky="ew", **pad)
        self.log_box = tk.Text(logf, height=9, width=62, state="disabled", font=("TkFixedFont", 9))
        self.log_box.grid()
        self._set_buttons(False)

    # -------------------------------------------------------------- UI helpers
    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_buttons(self, enabled):
        for b in self.motion_buttons:
            b.state(["!disabled"] if enabled else ["disabled"])

    def _show_position(self, steps):
        self.position = steps
        self.pos_steps.config(text=f"{steps:+d}")
        self.pos_mm.config(text=f"{steps * MM_PER_STEP:+.4f}")

    def _drain_ui(self):
        try:
            while True:
                kind, val = self.ui.get_nowait()
                if kind == "pos":
                    self._show_position(val)
                elif kind == "log":
                    self.log(val)
                elif kind == "state":
                    text, color = val
                    self.state_lbl.config(text=text, foreground=color)
                elif kind == "busy":
                    self.busy = val
                    self._set_buttons(not val and self.stage is not None)
                    self.connect_btn.state(["disabled"] if val else ["!disabled"])
                elif kind == "error":
                    messagebox.showerror("Stage", val)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_ui)

    def _refresh_ports(self):
        found = [] if self.simulate else gsc02.find_ports()
        ports = ["(simulated)"] if self.simulate else [d for d, _, _ in found]
        self.port_box["values"] = ports
        likely = [d for d, _, usb in found if usb]
        if likely and self.port_var.get() not in ports:
            self.port_var.set(likely[0])
        self.log(f"Ports: {', '.join(ports) or 'none found'}")

    # ------------------------------------------------------------ worker side
    def _worker(self):
        while True:
            name, fn = self.jobs.get()
            self.ui.put(("busy", True))
            try:
                fn()
            except StageError as e:
                self.ui.put(("log", f"ERROR ({name}): {e}"))
                self.ui.put(("state", ("Error", "red")))
                self.ui.put(("error", str(e)))
            except Exception as e:   # serial cable pulled, etc.
                self.ui.put(("log", f"ERROR ({name}): {e!r}"))
                self.ui.put(("state", ("Error", "red")))
                self.ui.put(("error", f"{name} failed: {e}"))
            finally:
                self.ui.put(("busy", False))

    def _submit(self, name, fn):
        if self.busy:
            return
        self.busy = True
        self.stop_requested = False
        self._set_buttons(False)
        self.jobs.put((name, fn))

    def _report_pos(self, steps):
        self.ui.put(("pos", steps))

    def _do_rezero(self):
        self.ui.put(("state", ("Re-zeroing (moving)...", "darkorange")))
        self.ui.put(("log", "Re-zero: homing to the limit switch, then out to zero..."))
        self.stage.home_and_zero(on_position=self._report_pos)
        self._report_pos(0)
        self.ui.put(("log", "Re-zero done: at the true zero."))
        self.ui.put(("state", ("Ready", "green")))

    def _do_move_to(self, target):
        """Move to target; returns True if it got there (False after STOP or a limit switch)."""
        if not in_travel(target):
            raise StageError(f"Target {target:+d} steps is outside the travel ({MIN_STEPS:+d} to {MAX_STEPS:+d}).")
        start = self.stage.position()
        if target == start:
            self.ui.put(("log", f"Already at {target:+d}."))
            return True
        self.ui.put(("state", ("Moving...", "darkorange")))
        self.stage.move_absolute(target)
        pos, ack2 = self.stage.wait_ready(on_position=self._report_pos, target=target)
        self._report_pos(pos)
        self.ui.put(("log", f"Moved {pos - start:+d} steps ({(pos - start) * MM_PER_STEP:+.4f} mm) "
                            f"-> {pos:+d}"))
        if ack2 in ("L", "W"):
            self.ui.put(("state", ("LIMIT SWITCH - re-zero", "red")))
            self.ui.put(("log", "WARNING: stopped at a limit switch. Re-zero before continuing."))
            return False
        if pos != target:
            self.ui.put(("state", ("Stopped", "darkorange")))
            self.ui.put(("log", f"Stopped at {pos:+d} before reaching {target:+d}."))
            return False
        self.ui.put(("state", ("Ready", "green")))
        return True

    def _do_max_scan(self):
        self.ui.put(("log", f"Max scan: {MAX_STEPS:+d} -> {MIN_STEPS:+d} -> 0"))
        for target in (MAX_STEPS, MIN_STEPS, 0):
            if self.stop_requested:
                break
            if not self._do_move_to(target):
                break
        else:
            self.ui.put(("log", "Max scan done."))
            return
        self.ui.put(("log", "Max scan ended early."))

    # ---------------------------------------------------------------- actions
    def connect(self):
        if self.busy:
            return
        port = self.port_var.get().strip()
        if not port and not self.simulate:
            self.log("No USB-serial adapter found. Plug in the stage, press 'Refresh ports' (or pick the\n"
                     "port from the list), then 'Connect + re-zero'.")
            self.state_lbl.config(text="No port", foreground="red")
            return

        def job():
            if self.stage:
                self.stage.close()
                self.stage = None
            self.ui.put(("state", ("Connecting...", "darkorange")))
            self.stage = GSC02(port, simulate=self.simulate)
            self.ui.put(("log", f"Connected on {port}. Count before re-zero: {self.stage.position():+d} steps."))
            self._do_rezero()
        self._submit("connect", job)

    def _size_steps(self):
        try:
            v = float(self.size_var.get())
        except ValueError:
            messagebox.showerror("Stage", "Step size must be a number.")
            return None
        steps = round(v / MM_PER_STEP) if self.unit_var.get() == "mm" else round(v)
        if steps <= 0:
            messagebox.showerror("Stage", "Step size must be positive (use Backward/Forward for direction).")
            return None
        return steps

    def jog(self, direction):
        n = self._size_steps()
        if n is not None:
            self.move_by(direction * n)

    def move_by(self, steps):
        if self.position is None:
            return
        target = self.position + steps
        if not in_travel(target):
            messagebox.showwarning("Stage", f"Target {target:+d} steps is outside the travel "
                                            f"({MIN_STEPS:+d} to {MAX_STEPS:+d} steps).")
            return
        self._submit("move", lambda: self._do_move_to(target))

    def go_zero(self):
        self._submit("go to zero", lambda: self._do_move_to(0))

    def max_scan(self):
        if not messagebox.askokcancel(
                "Max scan", f"The stage will move to the positive end ({MAX_STEPS:+d} steps, "
                            f"{MAX_STEPS * MM_PER_STEP:+.2f} mm), then the negative end ({MIN_STEPS:+d} steps, "
                            f"{MIN_STEPS * MM_PER_STEP:+.2f} mm), then back to zero.\n\nContinue?"):
            return
        self._submit("max scan", self._do_max_scan)

    def rezero(self):
        self._submit("re-zero", self._do_rezero)

    def stop(self):
        # Bypasses the job queue: the worker releases the serial port between
        # status polls, so this gets through within ~0.1 s.
        self.stop_requested = True
        if self.stage:
            self.stage.stop()
            self.log("STOP sent.")

    def _on_close(self):
        if self.busy and self.stage:
            self.stage.stop()
        if self.stage:
            self.stage.close()
        self.root.destroy()


def main():
    ap = argparse.ArgumentParser(description="Minimal GUI for the GSC-02 focus stage")
    ap.add_argument("--port", help="serial port, e.g. COM4 or /dev/ttyUSB0 (default: auto-detect)")
    ap.add_argument("--simulate", action="store_true", help="run without hardware")
    args = ap.parse_args()
    root = tk.Tk()
    StageApp(root, port=args.port, simulate=args.simulate)
    root.mainloop()


if __name__ == "__main__":
    main()
