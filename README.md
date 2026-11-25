# Stage Control GUI

A small desktop app for moving an **OptoSigma / Sigma Koki HPS60-20X**
translation stage through a **GSC-02** controller. It runs on Windows and
Linux with plain Python.

- Move single steps (about 1 µm) forward or backward, or any step size in steps or mm
- **Go to zero** button
- **Max scan** across the full travel: positive end, negative end, back to zero
- **Re-zero runs automatically at start-up**, and on demand
- Live position readout in steps and mm
- STOP button, and travel limits so the stage is never driven past its ends

> **The stage moves as soon as the app starts.** It drives to its home limit
> switch and back to zero (about 8 s). Make sure nothing is in its way.

---

## Requirements

| | |
|---|---|
| Python | 3.9 or newer (Tkinter is included with the standard installers) |
| Python package | `pyserial` |
| Hardware | GSC-02 controller, HPS60-20X stage, USB-to-serial adapter (RS-232C) |

---

## Installation: Windows

1. **Install Python.** Download it from <https://www.python.org/downloads/>.
   On the first screen of the installer, tick **"Add python.exe to PATH"**,
   then click *Install Now*.

2. **Get this code.** On the repository page click **Code → Download ZIP** and
   unzip it, or with Git installed:
   ```bat
   git clone https://github.com/AhableelTII/OptoSigma_GUI.git
   ```

3. **Install the serial package.** Open *Command Prompt* in the project
   folder (in File Explorer, type `cmd` in the address bar and press Enter):
   ```bat
   pip install -r requirements.txt
   ```

4. **Connect the stage.** Plug the USB-to-serial adapter into the PC and
   switch on the GSC-02. Open **Device Manager → Ports (COM & LPT)** and note
   the port, e.g. *Prolific USB-to-Serial Comm Port (COM4)*.
   If it shows a yellow warning icon or does not appear, install the adapter
   driver (for Prolific PL2303 adapters, from Prolific's website), then
   unplug and replug the adapter.

5. **Run it.** Double-click `run_stage_gui.bat`, or in Command Prompt:
   ```bat
   python stage_gui.py
   ```
   If it does not find the adapter, give the port explicitly:
   ```bat
   python stage_gui.py --port COM4
   ```

## Installation: Linux

```bash
git clone https://github.com/AhableelTII/OptoSigma_GUI.git
cd OptoSigma_GUI
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo apt install python3-tk          # if Tkinter is missing (Debian/Ubuntu)
sudo usermod -aG dialout $USER       # serial-port access; log out and back in once
.venv/bin/python stage_gui.py        # or: --port /dev/ttyUSB0
```

## Trying it without the stage

```bash
python stage_gui.py --simulate
```

This runs against a built-in simulated controller, so every button can be
tried with no hardware connected.

---

## Using it

| Control | What it does |
|---|---|
| **Step size** (steps or mm), presets 1 / 10 / 100 / 1000 | sets how far Backward and Forward move |
| **<< Backward (−)** / **Forward (+) >>** | move by the step size; negative is toward the home switch |
| **−1 step** / **+1 step** | move a single step (about 1 µm) |
| **Go to zero** | return to zero |
| **Re-zero (home switch)** | find the home switch again and rebuild zero |
| **Max scan** | move to the positive end (+9,900), then the negative end (−9,900), then back to zero; asks for confirmation first, and STOP ends the whole scan |
| **STOP** | stop the current move |

The position shown is measured from zero. Zero is defined as the home limit
switch plus 10,000 steps (about 10 mm), roughly the middle of the 20 mm travel.
Moves outside **−9,900 to +9,900 steps** (the full travel, 0.1 mm clear of
each end) are refused. If a move ends on a limit switch, the app warns you to
re-zero.

### Why re-zeroing matters

The stage has **no encoder**. The controller only counts the steps it sends,
so if the stage is moved by hand (with the knob), loses power, or stalls, the
count no longer matches the real position. Re-zeroing drives to the home
switch, which is the only physical reference, and rebuilds zero from there.
The app does this automatically at start-up. Press **Re-zero** after any
manual adjustment.

---

## Configuration

Settings are at the top of `gsc02.py`:

| Setting | Default | Meaning |
|---|---|---|
| `ZERO_OFFSET_STEPS` | 10000 | distance from the home switch to zero |
| `TRAVEL_STEPS` | 20000 | total travel (20 mm) |
| `EDGE_MARGIN_STEPS` | 100 | how far commands stay from each end |
| `MM_PER_STEP` | 0.0009996 | stage scale (about 1 µm per step) |

Serial settings: 9600 baud, 8 data bits, no parity, 1 stop bit.

## Troubleshooting

| Problem | Fix |
|---|---|
| "No port" in the window | plug in the adapter, press **Refresh ports**, pick it from the list, then **Connect + re-zero** |
| "No reply from a GSC-02" | check the controller is switched on, the serial cable is connected, and the right port is selected |
| `pip` or `python` not recognised (Windows) | reinstall Python with "Add python.exe to PATH" ticked |
| Permission denied on `/dev/ttyUSB0` (Linux) | add yourself to the `dialout` group (see above) and log in again |

## Files

| File | Purpose |
|---|---|
| `stage_gui.py` | the application window |
| `gsc02.py` | GSC-02 controller driver, settings, and the `--simulate` controller |
| `requirements.txt` | Python dependencies |
| `run_stage_gui.bat` | double-click launcher for Windows |
