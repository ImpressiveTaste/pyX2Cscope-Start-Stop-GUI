#!/usr/bin/env python3
"""
pyX2Cscope motor sequencer GUI
Start/stop the motor, live speed read-back.
Scale is treated as RPM per count (e.g. 0.19913 RPM/ct).
"""

import pathlib
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import serial.tools.list_ports

USE_SCOPE = True
if USE_SCOPE:
    from pyx2cscope.x2cscope import X2CScope

# MCAF variable paths
HWUI_VAR        = "app.hardwareUiEnabled"
VELOCITY_CMD    = "motor.apiData.velocityReference"
VELOCITY_MEAS   = "motor.apiData.velocityMeasured"
RUN_REQ_VAR     = "motor.apiData.runMotorRequest"
STOP_REQ_VAR    = "motor.apiData.stopMotorRequest"


# -------------------------------------------------------------------------
# Dummy replacements (enable by setting USE_SCOPE = False)
# -------------------------------------------------------------------------
class _DummyVar:
    def __init__(self, name):
        self.name = name
        self._val = 0

    def set_value(self, v):
        self._val = v
        print(f"[dummy] {self.name} = {v}")

    def get_value(self):
        return self._val


class _ScopeWrapper:
    def __init__(self):
        self._scope = None

    def connect(self, port, elf):
        if not USE_SCOPE:
            return
        self._scope = X2CScope(port=port)
        self._scope.import_variables(elf)

    def get_variable(self, path):
        if not USE_SCOPE:
            return _DummyVar(path)
        if self._scope is None:
            raise RuntimeError("Scope not connected")
        return self._scope.get_variable(path)

    def disconnect(self):
        if USE_SCOPE and self._scope:
            self._scope.disconnect()
            self._scope = None


# -------------------------------------------------------------------------
# Main GUI
# -------------------------------------------------------------------------
class MotorGUI:
    POLL_MS = 500  # speed read-back polling interval

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("pyX2Cscope – Motor Sequencer")

        self.scope = _ScopeWrapper()
        self.connected = False
        self._thread = None
        self._stop_flag = threading.Event()

        self._build_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_speeds()

    # ---------------------------------------------------------------------
    # GUI layout
    # ---------------------------------------------------------------------
    def _build_widgets(self):
        # Connection frame -------------------------------------------------
        conn = ttk.LabelFrame(self.root, text="Connection", padding=10)
        conn.pack(fill="x", padx=10, pady=6)

        ttk.Label(conn, text="ELF file:").grid(row=0, column=0, sticky="e")
        self.elf_path = tk.StringVar()
        ttk.Entry(conn, textvariable=self.elf_path, width=42)\
            .grid(row=0, column=1, padx=4, sticky="we")
        ttk.Button(conn, text="Browse…", command=self._browse_elf)\
            .grid(row=0, column=2, padx=4)

        ttk.Label(conn, text="COM port:").grid(row=1, column=0, sticky="e", pady=4)
        self.port_var = tk.StringVar()
        self.port_menu = ttk.OptionMenu(conn, self.port_var, "-", *self._ports())
        self.port_menu.grid(row=1, column=1, padx=4, sticky="we")
        ttk.Button(conn, text="↻", width=3, command=self._refresh_ports)\
            .grid(row=1, column=2, padx=4)

        self.conn_btn = ttk.Button(conn, text="Connect", command=self._toggle_conn)
        self.conn_btn.grid(row=2, column=0, columnspan=3, pady=(6, 2))

        # Parameters frame -------------------------------------------------
        parms = ttk.LabelFrame(self.root, text="Sequence parameters", padding=10)
        parms.pack(fill="x", padx=10, pady=4)

        def row(label: str, default: str, r: int):
            ttk.Label(parms, text=label).grid(row=r, column=0, sticky="e")
            e = ttk.Entry(parms, width=10)
            e.insert(0, default)
            e.grid(row=r, column=1, padx=6, pady=2)
            return e

        self.speed_entry = row("Speed (RPM):",      "1500",   0)
        self.scale_entry = row("Scale (RPM/cnt):",  "0.19913", 1)
        self.run_entry   = row("Run time (s):",     "10",     2)
        self.stop_entry  = row("Stop time (s):",    "50",     3)
        self.cycle_entry = row("Iterations:",       "3",      4)

        # Start/Stop buttons ----------------------------------------------
        btn_frm = ttk.Frame(self.root)
        btn_frm.pack(pady=(6, 2))

        self.start_btn = ttk.Button(btn_frm, text="START ▶",
                                    command=self._start_seq, width=12,
                                    state="disabled")
        self.start_btn.pack(side="left", padx=2)

        self.stop_btn = ttk.Button(btn_frm, text="STOP ■",
                                   command=self._stop_seq, width=12,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=2)

        # Status & read-back ----------------------------------------------
        self.status = tk.StringVar(value="Idle – not connected")
        ttk.Label(self.root, textvariable=self.status).pack(pady=(0, 4))

        read = ttk.Frame(self.root)
        read.pack(pady=(0, 8))

        ttk.Label(read, text="Measured speed:").grid(row=0, column=0, sticky="e")
        ttk.Label(read, text="Command speed:").grid(row=1, column=0, sticky="e")

        self.meas_str = tk.StringVar(value="—")
        self.cmd_str  = tk.StringVar(value="—")

        ttk.Label(read, textvariable=self.meas_str, width=22, anchor="w")\
            .grid(row=0, column=1, padx=6)
        ttk.Label(read, textvariable=self.cmd_str, width=22, anchor="w")\
            .grid(row=1, column=1, padx=6)

    # ---------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------
    @staticmethod
    def _ports():
        return [p.device for p in serial.tools.list_ports.comports()] or ["-"]

    def _refresh_ports(self):
        menu = self.port_menu["menu"]
        menu.delete(0, "end")
        for p in self._ports():
            menu.add_command(label=p, command=lambda v=p: self.port_var.set(v))
        self.port_var.set("-")

    def _browse_elf(self):
        fn = filedialog.askopenfilename(
            title="Select ELF file",
            filetypes=[("ELF files", "*.elf"), ("All files", "*.*")]
        )
        if fn:
            self.elf_path.set(fn)

    # ---------------------------------------------------------------------
    # Connection handling
    # ---------------------------------------------------------------------
    def _toggle_conn(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port, elf = self.port_var.get(), self.elf_path.get()

        if port in ("", "-") or not elf:
            messagebox.showwarning("Missing info", "Choose COM port and ELF file.")
            return
        if not pathlib.Path(elf).is_file():
            messagebox.showerror("File not found", "ELF file does not exist.")
            return

        try:
            self.scope.connect(port, elf)
            self.hwui_var  = self.scope.get_variable(HWUI_VAR)
            self.cmd_var   = self.scope.get_variable(VELOCITY_CMD)
            self.meas_var  = self.scope.get_variable(VELOCITY_MEAS)
            self.run_var   = self.scope.get_variable(RUN_REQ_VAR)
            self.stop_var  = self.scope.get_variable(STOP_REQ_VAR)
            self.hwui_var.set_value(0)  # disable HW UI on target
        except Exception as e:
            messagebox.showerror("Connection failed", str(e))
            return

        self.connected = True
        self.conn_btn.config(text="Disconnect")
        self.start_btn.config(state="normal")
        self.status.set(f"Connected ({port})")

    def _disconnect(self):
        if getattr(self, "stop_var", None):
            self.stop_var.set_value(1)
        self.scope.disconnect()

        self.connected = False
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.conn_btn.config(text="Connect")
        self.status.set("Disconnected")

    # ---------------------------------------------------------------------
    # Sequence control
    # ---------------------------------------------------------------------
    def _start_seq(self):
        if self._thread and self._thread.is_alive():
            return

        try:
            rpm    = float(self.speed_entry.get())
            scale  = float(self.scale_entry.get())   # RPM per count
            run_t  = float(self.run_entry.get())
            stop_t = float(self.stop_entry.get())
            cycles = int(self.cycle_entry.get())

            if scale <= 0 or run_t <= 0 or stop_t < 0 or cycles <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Input error", "Enter valid numeric values.")
            return

        if USE_SCOPE and not self.connected:
            messagebox.showwarning("Not connected", "Connect to a target first.")
            return

        self._stop_flag.clear()
        self.params = (rpm, scale, run_t, stop_t, cycles)

        self._thread = threading.Thread(target=self._run_sequence, daemon=True)
        self._thread.start()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

    def _stop_seq(self):
        if self._thread and self._thread.is_alive():
            self._stop_flag.set()
            self.stop_var.set_value(1)
            self.status.set("Stopping…")

    def _run_sequence(self):
        rpm, scale, run_t, stop_t, cycles = self.params
        cnt_cmd = int(round(rpm / scale))  # RPM → counts

        for n in range(1, cycles + 1):
            if self._stop_flag.is_set():
                break

            # RUN phase
            self.status.set(f"Cycle {n}/{cycles}: RUN @ {rpm:.0f} RPM")
            self.cmd_var.set_value(cnt_cmd)
            self.run_var.set_value(1)

            t0 = time.time()
            while time.time() - t0 < run_t:
                if self._stop_flag.is_set():
                    break
                time.sleep(0.05)

            if self._stop_flag.is_set():
                break

            # STOP phase
            self.status.set(f"Cycle {n}/{cycles}: STOP")
            self.stop_var.set_value(1)

            t0 = time.time()
            while time.time() - t0 < stop_t:
                if self._stop_flag.is_set():
                    break
                time.sleep(0.05)

        # ensure motor is stopped
        if getattr(self, "stop_var", None):
            self.stop_var.set_value(1)

        self.status.set(
            "Stopped by user" if self._stop_flag.is_set() else "Done – motor idle"
        )
        self.root.after(
            0, lambda: (
                self.start_btn.config(state="normal"),
                self.stop_btn.config(state="disabled"),
            )
        )

    # ---------------------------------------------------------------------
    # Speed polling
    # ---------------------------------------------------------------------
    def _poll_speeds(self):
        if self.connected:
            try:
                cnt_meas = self.meas_var.get_value()
                cnt_cmd  = self.cmd_var.get_value()
            except Exception:
                cnt_meas = cnt_cmd = 0

            try:
                scale = float(self.scale_entry.get())
                rpm_meas = cnt_meas * scale
                rpm_cmd  = cnt_cmd  * scale
                self.meas_str.set(f"{rpm_meas:+.0f} RPM ({cnt_meas})")
                self.cmd_str .set(f"{rpm_cmd:+.0f} RPM ({cnt_cmd})")
            except ValueError:
                self.meas_str.set("—")
                self.cmd_str.set("—")
        else:
            self.meas_str.set("—")
            self.cmd_str.set("—")

        self.root.after(self.POLL_MS, self._poll_speeds)

    # ---------------------------------------------------------------------
    # Clean-up
    # ---------------------------------------------------------------------
    def _on_close(self):
        try:
            if getattr(self, "stop_var", None):
                self.stop_var.set_value(1)
            self.scope.disconnect()
        finally:
            self.root.destroy()


# -------------------------------------------------------------------------
# Entry point
# -------------------------------------------------------------------------
if __name__ == "__main__":
    MotorGUI().root.mainloop()
