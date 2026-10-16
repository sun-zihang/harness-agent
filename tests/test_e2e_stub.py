"""End-to-end with a STUB model. No network, no live provider: proves the loop,
kernel, bootstrap tools, events and failover are wired correctly. If this passes,
the model is just a swappable text source.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, LIB)

from agent import Agent, extract_code                 # noqa: E402
from events import Events                             # noqa: E402
from kernel import Kernel                             # noqa: E402
from providers import Route, RouteError, Router        # noqa: E402
from tools import BOOTSTRAP                           # noqa: E402

REPO = os.path.dirname(HERE)
FAIL = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAIL.append(name)


# -- 1. bootstrap tools are live in the child --------------------------------
k = Kernel(workspace=REPO, bootstrap=BOOTSTRAP)
info = k.run("print(kernel_info())")
check("modelling engine loaded into kernel", "True" in info, info[:120])
k.run("note('assumed x>=0')")
check("note() round-trips from the child", k.last_notes == ["assumed x>=0"],
      repr(k.last_notes))
k.run("final_answer('REPORT-42')")
check("final_answer sets last_final", k.last_final == "REPORT-42", repr(k.last_final))
k.run("x=[1,2,3]")
check("state persists across blocks", k.run("print(len(x))").strip().startswith("3"),
      k.run("print(len(x))"))
k.close()

# -- 2. extract_code is tolerant of how models fence code --------------------
check("extract fenced python", "print(1)" in extract_code("intro ```python\nprint(1)\n``` tail"))
check("extract bare reply passes through", "x=1" in extract_code("x=1"))

# -- 3. full agent loop reaches final_answer through a stub model ------------
class StubRouter:
    def __init__(self):
        self.n = 0
        self.routes = []

    def chat(self, msgs, **kw):
        self.n += 1
        if self.n == 1:
            code = "print(solve_lp_max([3,4],[[2,1],[1,3],[1,1]],[100,90,60])['max_objective'])"
            return f"```python\n{code}\n```", [], {"route": "stub", "model": "m", "usage": {}}
        if self.n == 2:
            return "```python\nfinal_answer('optimum is 190')\n```", [], \
                {"route": "stub", "model": "m", "usage": {}}
        return "```python\nprint('extra')\n```", [], {"route": "stub", "model": "m", "usage": {}}


a = Agent(StubRouter(), workspace=REPO, max_steps=6, out="")
res = a.run("maximise 3x+4y")
check("loop returns the model's final_answer", "optimum is 190" in res, res[:120])
check("loop stops at final_answer (2 steps, not 6)", a.events.summary()["steps"] == 2,
      a.events.summary())

# -- 4. failover: dead route first, alive second -----------------------------
seen = {"alive": 0}


class Dead(Route):
    def chat(self, *a, **k):
        raise RouteError(502, "upstream down", "u", 1)


class Alive(Route):
    def chat(self, msgs, **k):
        seen["alive"] += 1
        return "ok", [], {"completion_tokens": 3}


led = os.path.join(HERE, "_led.json")
rt = Router([Dead(name="dead", base_url="http://x", api_key="k", model="m"),
             Alive(name="alive", base_url="http://y", api_key="k", model="m")],
            ledger_path=led)
txt, _, meta = rt.chat([{"role": "user", "content": "hi"}])
import json
rows = [v for day in json.load(open(led)).values() for v in day.values()]
check("failover served by the alive route", meta["route"] == "alive", meta)
check("ledger recorded the dead attempt as a failure",
      any(r.get("fail") for r in rows) and seen["alive"] == 1, rows)
os.remove(led)

# -- 5. all routes dead -> abort cleanly, no hang ----------------------------
class All:
    routes = []

    def chat(self, *a, **k):
        raise RuntimeError("nothing")


check("exhausted providers abort cleanly",
      "no working provider" in Agent(All(), workspace=REPO, max_steps=3, out="")
      .run("x"), "did not abort")

# -- 6. events persist to disk and resume ----------------------------------
p = os.path.join(HERE, "_e.jsonl")
os.path.exists(p) and os.remove(p)
e = Events(p)
e.emit("meta", task="t")
e.emit("step", step=1)
e.close()
r = Events(p, resume=True)
check("events replay from disk", len(list(r.iter())) == 2)
check("resume knows it continues (start=2)", r.start == 2, r.start)
r.close()
os.remove(p)

print(f"\n{'PASS' if not FAIL else 'FAIL'}: {len(FAIL)} failed"
      + (f" -> {FAIL}" if FAIL else ""))
sys.exit(1 if FAIL else 0)
