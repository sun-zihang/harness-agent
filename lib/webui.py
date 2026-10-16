"""Local web UI: stdlib-only server around the verified loop. No FastAPI, no node.

Why stdlib http.server: the repo's rule is that degradation must be honest and
zero-install; a pip-less fallback beats a prettier dependency for a localhost
tool. ThreadingHTTPServer + one worker thread per run + SSE tail of the same
append-only event records the CLI already writes -- the UI adds no second
source of truth.

The Agent was built around on_event(kind, step, text) and events.jsonl precisely
so a front-end could subscribe to this without touching the loop.

Security note that is not a lecture: binds 127.0.0.1 only. The button under this
page executes arbitrary code on this machine with no confirm step -- that is the
operator's stated design goal, but a LAN-exposed version of it would be a
backdoor for everyone else, so the bind is fixed, not configured.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

LIB = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKSPACE = os.path.dirname(LIB)

import sys
if LIB not in sys.path:
    sys.path.insert(0, LIB)
from agent import MODES          # sibling-flat import style, same as agent.py

MAX_BODY = 256 * 1024


class Run:
    """One agent run: thread + fan-out queue + the durable events file."""

    def __init__(self, rid, task, router_factory, workspace, max_steps, out_dir,
                 mode="general"):
        self.id, self.task = rid, task
        self.mode = mode
        self.status = "running"
        self.report = ""
        self.started = time.time()
        self.out_path = os.path.join(out_dir, f"{rid}.jsonl")
        self.subs = []                    # list[queue] -- SSE clients
        self._lock = threading.Lock()
        self._router_factory = router_factory
        self.workspace, self.max_steps = workspace, max_steps
        self.thread = threading.Thread(target=self._work, daemon=True)

    def publish(self, ev):
        with self._lock:
            for q in list(self.subs):
                q.put_nowait(ev)

    def subscribe(self):
        q = queue.Queue()
        # replay what already happened before attaching (open-then-race is how
        # live-tails usually drop the first events)
        if os.path.exists(self.out_path):
            with open(self.out_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        q.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        # The run can finish BEFORE a browser attaches (fast stub runs do exactly
        # that); its terminal publish then finds no subscriber, so snapshot the
        # terminal record into the fresh queue -- else the EventSource hangs
        # keep-aliving forever and the report panel never fills.
        if self.status != "running":
            q.put(self.status_rec())
        with self._lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self.subs:
                self.subs.remove(q)

    def _work(self):
        import sys
        sys.path.insert(0, LIB)
        from agent import Agent                        # noqa: E402

        agent = Agent(self._router_factory(), workspace=self.workspace,
                      max_steps=self.max_steps, out=self.out_path, mode=self.mode)
        # Bridge emit() -- not the loop's cosmetic on_event(kind, step, text),
        # which would duplicate records the durable stream already owns. Every
        # persisted event also fans out live, so the UI reads exactly what
        # events.jsonl does.
        _emit = agent.events.emit

        def emit(kind, **payload):
            rec = _emit(kind, **payload)
            self.publish(rec)
            if kind == "fatal":
                self.status = "error"
            return rec
        agent.events.emit = emit

        try:
            self.report = agent.run(self.task)
            if self.status == "running":
                self.status = ("done" if not self.report.startswith("[")
                               else "error")
        except Exception as exc:                       # surface, never hang the UI
            self.status = "crashed"
            self.report = f"[crashed] {type(exc).__name__}: {exc}"
        finally:
            self.publish(self.status_rec())

    def status_rec(self):
        return {"kind": "status", "status": self.status,
                "report": self.report[:200000],
                "secs": round(time.time() - self.started, 1)}


RUNS = {}
RUNS_LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    workspace = DEFAULT_WORKSPACE
    out_dir = os.path.join(DEFAULT_WORKSPACE, "out", "runs")
    # Set by make_server() -- which MUST wrap it in staticmethod(). A plain
    # function in a CLASS attribute becomes a bound method on instance access, so
    # `self.router_factory()` would get the handler as its first argument: a
    # TypeError deep in the run thread AFTER the POST already returned 200.
    # Caught by test_mode_http, not by reading the code.
    router_factory = None

    def log_message(self, *a):           # default stderr spam is worse than none
        pass

    # -- helpers ----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("content-length") or 0)
        if n <= 0 or n > MAX_BODY:
            return None
        raw = self.rfile.read(n)
        try:
            d = json.loads(raw.decode("utf-8"))
            return d if isinstance(d, dict) else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/api/health":
            os.environ.setdefault("PYTHONUTF8", "1")
            import sys
            sys.path.insert(0, LIB)
            from providers import load_routes
            rs = load_routes()
            return self._send(200, {
                "routes": [{"name": r.name, "base": r.base_url, "model": r.model}
                           for r in rs],
                "live": [r.name for r in rs if r.probe()]})
        if u.path == "/api/runs":
            with RUNS_LOCK:
                return self._send(200, [
                    {"id": r.id, "task": r.task[:160], "status": r.status,
                     "secs": round(time.time() - r.started, 1)}
                    for r in sorted(RUNS.values(), key=lambda x: -x.started)[:40]])
        if u.path == "/api/stream":
            rid = (parse_qs(u.query).get("run") or [""])[0]
            run = RUNS.get(rid)
            if not run:
                return self._send(404, {"error": "no such run"})
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-store")
            self.end_headers()
            q = run.subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        if run.status != "running" and q.empty():
                            # missed the terminal publish in the attach race
                            self.wfile.write(("data: " + json.dumps(
                                run.status_rec(), ensure_ascii=False)
                                + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                            break
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(
                        ("data: " + json.dumps(ev, ensure_ascii=False, default=str)
                         + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    if ev.get("kind") == "status":
                        break
            except (OSError, ValueError):
                pass                       # browser navigated away; that is normal
            finally:
                run.unsubscribe(q)
            return
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/api/run":
            return self._send(404, {"error": "not found"})
        body = self._read_json()
        if not body or not str(body.get("task", "")).strip():
            return self._send(400, {"error": "task required"})
        mode = str(body.get("mode") or "general")
        if mode not in MODES:                    # server-side: never trust the page
            return self._send(400, {"error": f"unknown mode {mode!r}"})
        os.makedirs(self.out_dir, exist_ok=True)
        rid = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6]
        run = Run(rid, body["task"].strip(), self.router_factory, self.workspace,
                  max(1, int(body.get("max_steps") or 40)), self.out_dir, mode)
        with RUNS_LOCK:
            RUNS[rid] = run
        run.thread.start()
        return self._send(200, {"id": rid})


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>harness</title><style>
body{margin:0;font:14px/1.55 ui-monospace,Menlo,Consolas,monospace;background:#101418;color:#d8dee4}
#wrap{display:grid;grid-template-columns:1fr 380px;height:100vh}
#main{padding:14px 18px;overflow:auto}#side{border-left:1px solid #2a3138;padding:14px;overflow:auto;background:#0c0f12}
h1{font-size:15px;margin:0 0 10px;color:#7ee2b8}
textarea{width:100%;box-sizing:border-box;background:#161b22;color:#d8dee4;border:1px solid #2a3138;border-radius:6px;padding:8px;font:inherit;min-height:64px}
button{background:#1f6feb;border:0;color:#fff;border-radius:6px;padding:7px 14px;cursor:pointer;font:inherit}
button:disabled{opacity:.45}
.ev{margin:6px 0;padding:8px 10px;border-radius:6px;border:1px solid #2a3138;background:#161b22;white-space:pre-wrap;word-break:break-word}
.ev .tag{font-size:11px;color:#8b949e}
.ev.err{border-color:#f85149}
pre{margin:6px 0 0}
#usage span{display:inline-block;background:#161b22;border:1px solid #2a3138;border-radius:5px;padding:2px 8px;margin:2px 4px 2px 0;font-size:12px}
#runs div{cursor:pointer;padding:3px 0;color:#58a6ff}
#final{border-color:#7ee2b8}
</style></head><body><div id="wrap">
<div id="main"><h1>harness · 零审批执行体 <span id="health" style="font-size:12px;color:#8b949e"></span></h1>
<textarea id="task" placeholder="任务，例如：最大化 3x+4y，约束 2x+y<=100, x+3y<=90, x+y<=60，并做灵敏度分析"></textarea>
<div style="margin:8px 0"><button id="go">执行</button>
<label style="font-size:12px;color:#8b949e">模式
<select id="mode" style="background:#161b22;color:#d8dee4;border:1px solid #2a3138;border-radius:4px">
<option value="general">通用</option><option value="competition">数赛</option></select></label>
<label style="font-size:12px;color:#8b949e">max_steps <input id="steps" type="number" value="40" style="width:52px;background:#161b22;color:#d8dee4;border:1px solid #2a3138;border-radius:4px"></label></div>
<div id="stream"></div><div id="final" class="ev" style="display:none"></div></div>
<div id="side"><h1>路由</h1><div id="usage"></div><h1 style="margin-top:16px">历史</h1><div id="runs"></div></div>
</div><script>
const $=s=>document.querySelector(s),esc=s=>(s+'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function health(){$('#usage').innerHTML='<span>检查中…</span>';try{const h=await(await fetch('/api/health')).json();
$('#usage').innerHTML=(h.routes.length?h.routes.map(r=>`<span>${esc(r.name)} ${h.live.includes(r.name)?'·活':'·挂'}</span>`).join(''):'<span>无路由：配 env 或端点</span>');
$('#health').textContent=h.live.length?'':`(无在线路由)`}catch(e){$('#usage').textContent='服务不可达'}}
async function runs(){$('#runs').innerHTML='';for(const r of(await(await fetch('/api/runs')).json())){const d=document.createElement('div');d.textContent=r.id.slice(0,12)+' · '+r.status+' · '+r.secs+'s';d.title=r.task;d.onclick=()=>location.hash=r.id;$('#runs').append(d)}}
function add(ev){const d=document.createElement('div');d.className='ev'+(ev.is_error?' err':'');
const tag={step:'模型 → 代码',observation:'内核输出',fatal:'致命',meta:'开始'}[ev.kind]||ev.kind;
let b=esc(ev.code||ev.out||ev.error||ev.task||'');if(b.length>4000)b=b.slice(0,4000)+'…';
d.innerHTML=`<span class="tag">${esc(tag)}${ev.step?' #'+ev.step:''}${ev.route?' ['+esc(ev.route)+']':''}</span><pre>${b}</pre>`;$('#stream').append(d);$('#main').scrollTop=1e9}
let es=null;
$('#go').onclick=async()=>{const t=$('#task').value.trim();if(!t)return;
$('#go').disabled=true;$('#stream').innerHTML='';$('#final').style.display='none';
const r=await(await fetch('/api/run',{method:'POST',headers:{'content-type':'application/json'},
body:JSON.stringify({task:t,max_steps:+$('#steps').value||40,mode:$('#mode').value})})).json();
if(r.error){add({kind:'fatal',error:r.error});$('#go').disabled=false;return}
history.replaceState(null,'',r.id);if(es)es.close();es=new EventSource('/api/stream?run='+r.id);
es.onmessage=e=>{const ev=JSON.parse(e.data);if(ev.kind=='status'){$('#go').disabled=false;
$('#final').style.display='';$('#final').innerHTML=`<span class="tag">完成 · ${ev.secs}s · ${esc(ev.status)}</span><pre>${esc(ev.report)}</pre>`;
runs();es.close()}else add(ev)};es.onerror=()=>{}};
health();runs();setInterval(()=>{runs()},5000);
</script></body></html>"""


def make_server(port=0, router_factory=None, workspace=None, out_dir=None):
    import sys
    sys.path.insert(0, LIB)
    if router_factory is None:
        from providers import load_routes
        router_factory = lambda: __import__("providers").Router(load_routes())
    workspace = workspace or DEFAULT_WORKSPACE
    hs = Handler
    hs.router_factory = staticmethod(router_factory)
    hs.workspace = workspace
    hs.out_dir = out_dir or os.path.join(workspace, "out", "runs")
    srv = ThreadingHTTPServer(("127.0.0.1", port), hs)
    return srv


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    srv = make_server(a.port)
    print(f"harness UI: http://127.0.0.1:{srv.server_address[1]}  (localhost only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()      # shutdown() alone left the listening socket bound


if __name__ == "__main__":
    main()
