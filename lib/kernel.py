"""CodeAct kernel: the model acts by writing python, not JSON tool-calls.

Borrowed from smolagents/OpenHands: an action IS code run in a persistent
namespace, so there is no tool-call enum between intent and effect -- which is
also why there is no place a confirmation gate could sit. The difference here is
that tools are injected as *real callables* into the kernel namespace: the model
calls model_run(...) like a local function instead of emitting JSON that a parser
might mis-read. Fewer moving parts, fewer silent parse failures.

Fixes deliberately inherited from hard-won lessons:
  * the child loop is one compile/exec path -- an earlier fallback branch that
    tried to 'recover' from SyntaxError was itself broken; a syntax error is just
    an exception reported back, nothing exotic.
  * state persists across exec() (a lost namespace silently breaks multi-step
    work, so test_persistence asserts it).
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout


class Kernel:
    """Long-lived python subprocess with a persistent namespace.

    Falls back to one-shot `python -c` if the pipe cannot start, so the agent
    still runs -- just without state carry-over (and it says so).
    """

    def __init__(self, python=None, workspace=None, bootstrap=""):
        self.python = python or sys.executable or "python"
        self.workspace = workspace or os.getcwd()
        self.bootstrap = bootstrap
        self.proc = None
        self.persistent = False
        self.seeded = False
        self.last_final = None

    def start(self):
        if self.proc and self.proc.poll() is None and self.seeded:
            return
        try:
            self.proc = subprocess.Popen(
                [self.python, "-u", "-c", _CHILD],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,      # child reports errors in the JSON
                text=True, encoding="utf-8", bufsize=1, cwd=self.workspace)
            self.persistent = True
            self.seeded = False
            if self.bootstrap:
                self._seed()
            else:
                self.seeded = True
        except OSError:
            self.proc, self.persistent, self.seeded = None, False, False

    def _seed(self):
        """Inject tool defs + imports as the very first frame and wait for the
        ack, so the model can call model-authored helpers from step one. A seed
        that fails means the tools are broken -- surface it, never run codeblind.
        """
        out = self._pipe(self.bootstrap, timeout=60)
        if out is None or "[raised]" in out or "Traceback" in (out or ""):
            raise RuntimeError(f"kernel bootstrap failed:\n{out}")
        self.seeded = True

    def run(self, code, timeout=180):
        self.start()
        if self.persistent and self.proc and self.proc.poll() is None and self.seeded:
            out = self._pipe(code, timeout)
            if out is not None:
                return out
            self.restart()                    # pipe died -> one-shot this call
        return self._oneshot(code, timeout)

    def _pipe(self, code, timeout):
        box = {}

        def reader():
            box["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=reader, daemon=True)
        try:
            self.proc.stdin.write(json.dumps({"code": code}) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return None
        t.start()
        t.join(timeout)
        if t.is_alive():
            return f"[timeout after {timeout}s] namespace may be mid-execution; restarting"
        line = box.get("line")
        if not line:
            return None
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return None
        out = (d.get("stdout") or "") + (d.get("stderr") or "")
        if d.get("error"):
            out += f"\n[raised]\n{d['error']}"
        self.last_final = d.get("final")       # set when the code called final_answer
        self.last_notes = d.get("notes") or []  # note() calls, read back from the child
        return out[:60000] or "[no output]"

    def _oneshot(self, code, timeout):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        try:
            r = subprocess.run([self.python, path], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, cwd=self.workspace)
            return ((r.stdout or "") + (r.stderr or ""))[:60000] or "[no output]"
        except subprocess.TimeoutExpired:
            return f"[timeout after {timeout}s]"
        finally:
            os.unlink(path)

    def restart(self):
        self._kill()
        self.start()                        # caller wants a live, re-seeded kernel

    def _kill(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.proc, self.persistent, self.seeded = None, False, False

    def close(self):
        # Must NOT restart: an earlier close()->restart() spawned a fresh child
        # and then dropped the handle, stranding a live subprocess -- the exact
        # process-leak a host like ZCode would report.
        self._kill()


_CHILD = r'''
import sys, json, io, traceback
from contextlib import redirect_stdout, redirect_stderr


class FinalAnswer(Exception):
    def __init__(self, text):
        self.text = text


ns = {"__name__": "__harness__", "FinalAnswer": FinalAnswer}
for raw in sys.stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        job = json.loads(raw)
    except json.JSONDecodeError:
        continue
    so, se = io.StringIO(), io.StringIO()
    err = None
    final = None
    try:
        with redirect_stdout(so), redirect_stderr(se):
            exec(compile(job["code"], "<harness>", "exec"), ns)
    except FinalAnswer as f:                # clean stop, not an error
        final = f.text
    except BaseException:
        err = traceback.format_exc(limit=8)
    sys.stdout.write(json.dumps(
        {"stdout": so.getvalue()[-200000:], "stderr": se.getvalue()[-200000:],
         "error": err, "final": final,
         "notes": ns.get("__notes__")}) + "\n")
    sys.stdout.flush()
'''
