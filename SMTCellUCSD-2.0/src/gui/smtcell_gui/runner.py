"""QProcess wrapper that streams `make` stdout/stderr line by line.

Self-contained (PySide6 + a cwd Path): line-streaming, cancel, elapsed
timing. The child make/solver is isolated from the GUI so it can never be
collaterally killed or pick up the GUI's Qt/display: a headless matplotlib
backend, no inherited Qt platform, and its own process session/group.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


class MakeRunner(QObject):
    """Run one `make <target> [K=V ...]` invocation and stream output.

    Emits one `line` signal per line (stdout and stderr merged), then a
    single `finished` signal with the exit code and elapsed wall time.
    Only one run may be in-flight at a time.
    """

    line = Signal(str)
    started_at = Signal(float)            # epoch seconds
    finished = Signal(int, float)         # exit_code, elapsed_seconds
    error = Signal(str)                   # spawn/IO failure

    def __init__(self, cwd: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cwd = cwd
        self._proc: QProcess | None = None
        self._t0: float = 0.0
        self._buf_out = ""
        self._buf_err = ""
        self._cancelling = False

    @property
    def is_running(self) -> bool:
        p = self._proc
        return p is not None and p.state() != QProcess.NotRunning

    def start(self, target: str, kv: dict[str, str]) -> None:
        if self.is_running:
            self.error.emit("a run is already in progress")
            return

        proc = QProcess(self)
        proc.setWorkingDirectory(str(self._cwd))
        proc.setProcessChannelMode(QProcess.MergedChannels)

        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONUNBUFFERED", "1")
        # The solver writes its layout PNG with matplotlib. Force the headless
        # Agg backend and an offscreen Qt platform so the child never tries to
        # touch the GUI's X display / Qt (which can abort the child, e.g. when
        # PySide6 is importable in the env or the GUI runs over X-forwarding).
        env.insert("MPLBACKEND", "Agg")
        env.insert("QT_QPA_PLATFORM", "offscreen")
        proc.setProcessEnvironment(env)

        # Put the child in its own session/process group so a signal sent to
        # the GUI's group (terminal job control, a stray SIGINT/SIGHUP, etc.)
        # cannot collaterally kill the long-running solver - only an explicit
        # cancel() does. Best-effort: if setsid is unavailable, fall back.
        try:
            proc.setChildProcessModifier(lambda: os.setsid())
        except (AttributeError, OSError):
            pass

        # `make <tgt> KEY=VAL KEY2=VAL2 ...`
        args = [target] + [f"{k}={v}" for k, v in kv.items() if v != ""]

        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_finished)
        proc.errorOccurred.connect(self._on_error)

        self._proc = proc
        self._buf_out = ""
        self._buf_err = ""
        self._cancelling = False
        self._t0 = time.monotonic()

        self.started_at.emit(time.time())
        self.line.emit(f"$ make {' '.join(args)}")
        proc.start("make", args)

    def cancel(self) -> None:
        p = self._proc
        if p is None or p.state() == QProcess.NotRunning:
            return
        self._cancelling = True
        self.line.emit("[cancel requested]")
        # The child is its own group leader (setsid), so signal the whole group
        # to also stop the python solver beneath make. Fall back to QProcess if
        # the pid/group is unavailable.
        pid = int(p.processId())
        if pid > 0:
            self._signal_group(pid, signal.SIGTERM) or p.terminate()
            if not p.waitForFinished(2000):
                self.line.emit("[terminate timed out, killing]")
                self._signal_group(pid, signal.SIGKILL) or p.kill()
        else:
            p.terminate()
            if not p.waitForFinished(2000):
                p.kill()

    @staticmethod
    def _signal_group(pid: int, sig: int) -> bool:
        """Send `sig` to the child's process group (pgid == pid via setsid).
        Returns True on success, False if it could not (caller falls back)."""
        try:
            os.killpg(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _on_stdout(self) -> None:
        self._drain(self._proc.readAllStandardOutput().data().decode(
            "utf-8", errors="replace"), use_err=False)

    def _on_stderr(self) -> None:
        self._drain(self._proc.readAllStandardError().data().decode(
            "utf-8", errors="replace"), use_err=True)

    def _drain(self, chunk: str, use_err: bool) -> None:
        # Stream complete lines; hold the trailing partial line in a buffer.
        if use_err:
            buf = self._buf_err + chunk
        else:
            buf = self._buf_out + chunk
        *lines, tail = buf.split("\n")
        for ln in lines:
            self.line.emit(ln)
        if use_err:
            self._buf_err = tail
        else:
            self._buf_out = tail

    def _flush_tails(self) -> None:
        for tail in (self._buf_out, self._buf_err):
            if tail:
                self.line.emit(tail)
        self._buf_out = ""
        self._buf_err = ""

    def _on_finished(self, exit_code: int, status: object) -> None:
        self._flush_tails()
        elapsed = time.monotonic() - self._t0
        # A CrashExit means the child was killed by a signal (exit_code is the
        # signal number), NOT a make error - say so plainly instead of "exit N".
        if status == QProcess.ExitStatus.CrashExit and not self._cancelling:
            self.line.emit(
                f"[terminated by signal {exit_code} — not a make error; "
                f"likely out-of-memory, a ulimit, or an external signal]")
        # Detach + schedule deletion of THIS finished process BEFORE emitting
        # `finished`. The emit can synchronously start the next chained stage
        # (Run all), which assigns a fresh self._proc; if we cleaned up after
        # the emit we would deleteLater() that NEW, RUNNING process - and
        # destroying a running QProcess kills it (the spurious 'signal 9' that
        # hit stage 2 of every Run-all). Clearing self._proc first also keeps
        # the re-entrant start()'s is_running check correct.
        proc = self._proc
        self._proc = None
        if proc is not None:
            proc.deleteLater()
        self.finished.emit(exit_code, elapsed)

    def _on_error(self, err: QProcess.ProcessError) -> None:
        # A `Crashed` error is always paired with `finished` (terminate/kill on
        # cancel, or a signal-killed solver) - let `_on_finished` report it, and
        # stay silent for a deliberate cancel. Only a `FailedToStart` (e.g. make
        # not on PATH) fires WITHOUT `finished`, so it must be surfaced and the
        # dead QProcess cleaned up here (otherwise it leaks).
        if err == QProcess.ProcessError.Crashed or self._cancelling:
            return
        self.error.emit(f"QProcess error: {err}")
        if self._proc is not None and self._proc.state() == QProcess.NotRunning:
            self._proc.deleteLater()
            self._proc = None
