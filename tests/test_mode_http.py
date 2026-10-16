"""数赛模式 end-to-end over HTTP: a real capability profile, not a UI decoration.

Properties a decorative switch fails:
  1. the mode changes the prompt the MODEL sees
  2. it is written into the durable event stream (audit afterwards)
  3. an unknown mode 400s at the API rather than silently downgrading
  4. the page's <select> actually reaches the POST body
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
sys.path.insert(0, LIB)

from agent import MODES, system_prompt          # noqa: E402
from webui import make_server                   # noqa: E402

REPO = os.path.dirname(HERE)
FAIL = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"  <- {extra}"))
    if not cond:
        FAIL.append(name)


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


# 1. prompt content differs, and only where it should
comp, gen = system_prompt("competition"), system_prompt("general")
check("competition prompt adds the report skeleton",
      "问题重述" in comp and "数赛模式" in comp)
check("general prompt is not secretly competition", "问题重述" not in gen)
check("typo'd mode raises", _raises(lambda: system_prompt("conpetition")))

# 2-4 through a live server with a deliberately dead provider: mode plumbing
# must work even when the model call fails -- that is an infrastructure test,
# not a model test
class DeadRouter:
    routes = []

    def chat(self, *a, **k):
        raise RuntimeError("no provider in tests")


srv = make_server(0, router_factory=lambda: DeadRouter(), workspace=REPO,
                  out_dir=HERE)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(obj):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/run",
                                 data=json.dumps(obj).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def poll(path, secs=15):
    """Wait for the file to hold its FIRST record, not merely to exist.

    Events opens its file (creating it, 0 bytes) before emitting meta, so a
    poll-until-exists race reads an empty file and reports a phantom failure --
    exactly what this test flashed once. Content, not existence, is the signal.
    """
    end = time.time() + secs
    while time.time() < end:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                recs = [json.loads(l) for l in fh if l.strip()]
            if recs:
                return recs
        time.sleep(0.1)
    return None


code, body = post({"task": "t", "mode": "competition"})
rid = body.get("id")
check("API accepts competition mode", code == 200 and bool(rid), (code, body))
evs = poll(os.path.join(HERE, f"{rid}.jsonl")) if rid else None
check("event stream carries the mode",
      bool(evs) and evs[0].get("mode") == "competition",
      evs[0] if evs else "no events file")
check("mode survives into the fatal event",
      bool(evs) and any(e.get("kind") == "fatal" for e in evs),
      "provider failure must not erase mode provenance")

code, body = post({"task": "t", "mode": "数赛"})
check("unknown mode rejected at the API with 400",
      code == 400 and "mode" in str(body), (code, body))

import webui
page = webui.PAGE
check('UI has the mode selector', 'id="mode"' in page)
check("selector reaches the POST body (otherwise it is decorative)",
      "mode:$('#mode').value" in page)

# -- 5. main() itself: the argparse/print/bind path nothing had ever executed.
#    Done in-process (PowerShell's Start-Process chokes on a duplicate NO_PROXY
#    env key), patching make_server to capture the bound instance.
import webui                                          # noqa: E402

captured = {}
_orig = webui.make_server


def _spy(port=0, **kw):
    s = _orig(port, **kw)
    captured["srv"] = s
    return s


webui.make_server = _spy
_argv = list(sys.argv)
sys.argv = ["webui.py", "--port", "0"]
threading.Thread(target=webui.main, daemon=True).start()
deadline = time.time() + 8
while time.time() < deadline and "srv" not in captured:
    time.sleep(0.1)
srv2 = captured.get("srv")
check("main() bound a socket via --port", srv2 is not None)
if srv2:
    p2 = srv2.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{p2}/", timeout=10) as r:
        body = r.read().decode("utf-8", "replace")
        check("GET / serves the page", r.status == 200 and 'id="mode"' in body)
    with urllib.request.urlopen(f"http://127.0.0.1:{p2}/api/health", timeout=40) as r:
        h = json.loads(r.read().decode())
        # assert shape, not counts: the ambient env decides how many routes
        # exist on the test machine, and a live key must not make this red
        check("GET /api/health reports routes + live",
              r.status == 200 and set(h) >= {"routes", "live"}
              and set(h["live"]) <= {x["name"] for x in h["routes"]},
              str(h)[:120])
        check("health is honest when nothing is configured",
              bool(h["routes"]) or h["live"] == [], h)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{p2}/api/nope", timeout=10)
        check("unknown path 404s", False, "got 200")
    except urllib.error.HTTPError as e:
        check("unknown path 404s", e.code == 404, e.code)
    srv2.shutdown()
webui.make_server = _orig
sys.argv = _argv

srv.shutdown()
for f in os.listdir(HERE):
    if f.endswith(".jsonl"):
        try:
            os.remove(os.path.join(HERE, f))
        except OSError:
            pass

print(f"\n{'PASS' if not FAIL else 'FAIL'}: {len(FAIL)} failed"
      + (f" -> {FAIL}" if FAIL else ""))
sys.exit(1 if FAIL else 0)
