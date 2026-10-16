"""Event stream: JSONL is the source of truth; state is derived from it.

Borrowed from OpenHands: every action, observation, and model call is an
append-only event. That buys three things a bare loop never gives you:
  * replay -- re-render what happened, including which route served each step
  * rewind  -- truncate to step N and continue from the kernel state as of then
    (kernel rewind is bounded: a python namespace cannot be un-executed, so
    rewinding re-runs from the last checkpoint instead of pretending to undo)
  * audit   -- a report can cite "event 17 produced 190.0", which is how a
    competition number stays traceable weeks later.
"""
from __future__ import annotations

import json
import os
import time


class Events:
    def __init__(self, path, resume=False):
        self.path = path
        self._mem = []
        self.start = 0
        if path and not resume and os.path.exists(path):
            stem, ext = os.path.splitext(path)
            self.path = f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        if path and resume and os.path.exists(path):
            # Load prior events into _mem BEFORE anything calls iter(). An
            # earlier version computed start via self.iter() while _mem was still
            # unset, so every resume raised AttributeError -- fixed by ordering,
            # not by a defensive guard that would hide the real defect.
            self._mem = self._read_disk(path)
            self.start = len(self._mem)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8") if path else None
        self._n = self.start

    @staticmethod
    def _read_disk(path):
        out = []
        if not path or not os.path.exists(path):
            return out
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return out

    def emit(self, kind, **payload):
        self._n += 1
        rec = {"seq": self._n, "t": round(time.time(), 3), "kind": kind, **payload}
        self._mem.append(rec)
        if self._fh:
            self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()          # separate statement on purpose: `write() or
                                      # flush()` never flushes -- write() returns a
                                      # truthy char count. This exact bug hung an
                                      # MCP server for two debug rounds.
        return rec

    def iter(self):
        """Disk is the durable copy; memory is the live mirror. Read whichever
        has content so resume sees prior events and out='' still reports them."""
        if self._mem:
            yield from self._mem
            return
        if not self.path or not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    def upto(self, n):
        return [e for e in self.iter() if e["seq"] <= n]

    def steps(self):
        return [e for e in self.iter() if e["kind"] == "step"]

    def summary(self):
        evs = list(self.iter())
        routes = {}
        for e in evs:
            if e["kind"] == "step" and e.get("route"):
                routes[e["route"]] = routes.get(e["route"], 0) + 1
        return {"events": len(evs), "steps": sum(1 for e in evs if e["kind"] == "step"),
                "errors": sum(1 for e in evs if e.get("is_error")),
                "routes_used": routes,
                "tokens": sum((e.get("usage") or {}).get("completion_tokens", 0)
                              + (e.get("usage") or {}).get("output_tokens", 0)
                              for e in evs)}

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None
