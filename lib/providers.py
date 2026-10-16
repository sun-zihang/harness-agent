"""Pluggable model endpoints: any OpenAI/Anthropic-compatible base_url + key.

This is the "connect many channels" layer, and its scope is deliberate. A route
is just an endpoint + credential. Sources of routes:

  * env / a keys file / literal config  -- YOUR OWN keys (OpenAI, DeepSeek,
    Z.ai, OpenRouter, a local ollama, ...)
  * a running local gateway you already operate (e.g. a jet-hub OpenAI endpoint
    on 127.0.0.1) -- consumed as a plain chat-completions server.

What this file is NOT: an account pool, a token refresher, or a check-in
automation for any vendor. Those are the parts of a gateway that reach into a
third party's free-tier/abuse systems, and they are intentionally absent -- this
module only POSTs /chat/completions to endpoints you point it at, exactly like it
would POST to api.openai.com. Borrowing the *shape* of a gateway (multi-route,
failover, per-model usage ledger) without the *harvesting* is the whole design.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


def _expand(p: str) -> str:
    return os.path.expandvars(os.path.expanduser((p or "").strip()))


@dataclass
class Route:
    name: str
    base_url: str
    api_key: str
    model: str
    kind: str = "openai"                # openai | anthropic
    weight: int = 0                     # lower = tried first
    extra_headers: dict = field(default_factory=dict)
    note: str = ""

    RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

    # -- wire -------------------------------------------------------------
    def _post(self, url, payload, headers, timeout, retries):
        last = None
        for attempt in range(1, retries + 1):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8")), None
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:1500]
                last = RouteError(e.code, body, url, attempt)
                if e.code not in self.RETRY_STATUS:
                    raise last from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = RouteError(0, f"{type(e).__name__}: {e}", url, attempt)
            if attempt < retries:
                time.sleep(min(6.0, 0.5 * (2 ** attempt)))
        raise last

    def chat(self, messages, *, temperature=0.3, max_tokens=8192,
             timeout=240, retries=3):
        if self.kind == "anthropic":
            return self._chat_anthropic(messages, temperature, max_tokens, timeout, retries)
        return self._chat_openai(messages, temperature, max_tokens, timeout, retries)

    def _chat_openai(self, messages, temperature, max_tokens, timeout, retries):
        url = self.base_url.rstrip("/") + "/chat/completions"
        d, _ = self._post(
            url,
            {"model": self.model, "messages": messages,
             "temperature": temperature, "max_tokens": max_tokens},
            {"content-type": "application/json",
             "authorization": f"Bearer {self.api_key}", **self.extra_headers},
            timeout, retries)
        msg = d["choices"][0]["message"]
        return msg.get("content") or "", msg.get("tool_calls") or [], d.get("usage", {})

    def _chat_anthropic(self, messages, temperature, max_tokens, timeout, retries):
        base = self.base_url.rstrip("/")
        url = (base if base.endswith("/v1") else base + "/v1") + "/messages"
        sys_txt = " ".join(m["content"] for m in messages if m["role"] == "system")
        body = [m for m in messages if m["role"] != "system"]
        d, _ = self._post(
            url,
            {"model": self.model, "system": sys_txt or None, "messages": body,
             "max_tokens": max_tokens, "temperature": temperature},
            {"content-type": "application/json", "x-api-key": self.api_key,
             "anthropic-version": "2023-06-01", **self.extra_headers},
            timeout, retries)
        txt = "".join(b.get("text", "") for b in d.get("content", [])
                      if b.get("type") == "text")
        return txt, [], d.get("usage", {})

    def probe(self, timeout=6):
        """Cheap reachability check. Never raises -- used to rank routes."""
        try:
            url = self.base_url.rstrip("/")
            url = url + "/models" if self.kind == "openai" else url
            req = urllib.request.Request(
                url, headers={"authorization": f"Bearer {self.api_key}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()
            return True
        except Exception:
            return False


class RouteError(RuntimeError):
    def __init__(self, status, body, url, attempts):
        self.status, self.body, self.url, self.attempts = status, body, url, attempts
        super().__init__(f"HTTP {status or 'transport'} x{attempts} @ {url}: {body[:300]}")


# -- routing / failover ---------------------------------------------------
class Router:
    """Ordered routes + per-route usage ledger.

    The ledger is borrowed from gateway practice for a benign reason: an agent
    that fails over silently will quietly drain whichever endpoint it keeps
    hitting. Recording attempts/route/model makes 'which channel served this and
    at what cost' answerable, which is also the anti-abuse property -- you can
    see what you actually used instead of assuming.
    """

    def __init__(self, routes, ledger_path=None):
        self.routes = sorted(routes, key=lambda r: r.weight)
        self.ledger_path = ledger_path
        self.ledger = self._load()

    def _load(self):
        if self.ledger_path and os.path.exists(self.ledger_path):
            try:
                with open(self.ledger_path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self):
        if not self.ledger_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.ledger_path)) or ".", exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as fh:
            json.dump(self.ledger, fh, ensure_ascii=False, indent=2)

    def _record(self, route, ok, err=None, usage=None):
        day = time.strftime("%Y-%m-%d")
        key = f"{route.name}|{route.model}"
        row = self.ledger.setdefault(day, {}).setdefault(
            key, {"calls": 0, "ok": 0, "fail": 0, "in_tokens": 0, "out_tokens": 0})
        row["calls"] += 1
        row["ok" if ok else "fail"] += 1
        if usage:
            row["in_tokens"] += int(usage.get("prompt_tokens")
                                    or usage.get("input_tokens") or 0)
            row["out_tokens"] += int(usage.get("completion_tokens")
                                     or usage.get("output_tokens") or 0)
        if err:
            row["last_error"] = str(err)[:200]
        self._save()

    def chat(self, messages, *, prefer=None, **kw):
        """Try routes in order until one answers. Records the winner.

        'prefer' pins a name/model but does NOT exclude others: a pinned route
        that is down still falls over, because a headless loop must not die on
        one endpoint being off (the failure mode a fixed single route invites).
        """
        order = self.routes
        if prefer:
            order = ([r for r in self.routes if prefer in (r.name, r.model)]
                     + [r for r in self.routes if prefer not in (r.name, r.model)])
        errs = []
        for route in order:
            try:
                txt, tcalls, usage = route.chat(messages, **kw)
                self._record(route, True, usage=usage)
                return txt, tcalls, {"route": route.name, "model": route.model,
                                     "usage": usage}
            except RouteError as e:
                self._record(route, False, err=e)
                errs.append(f"{route.name}: {e}")
        raise NoRoute(f"every route failed:\n  " + "\n  ".join(errs))

    def summary(self):
        return self.ledger

    def live_routes(self):
        return [r.name for r in self.routes if r.probe()]


class NoRoute(RuntimeError):
    pass


# -- sources: how routes get their endpoints/keys -------------------------
def load_routes(cfg=None, env=None):
    """Build routes from three benign sources. No vendor login, no refresh."""
    e = env or os.environ
    cfg = cfg or {}
    routes = []

    # 1. literal / named endpoints in config
    for spec in cfg.get("endpoints", []):
        routes.append(Route(
            name=spec.get("name") or _host(spec["base_url"]),
            base_url=_expand(spec["base_url"]),
            api_key=_resolve_key(spec, e),
            model=spec.get("model", ""),
            kind=spec.get("kind", "openai"),
            weight=int(spec.get("weight", 0)),
            extra_headers=spec.get("headers", {}),
            note=spec.get("note", ""),
        ))

    # 2. env shorthands for common providers you hold keys for. Ollama is opt-in
    #    (only when an OLLAMA_* var is set): auto-adding a local endpoint that is
    #    usually not running would make every failover stall on connection-refused
    #    first. A dead route is skipped, but skipping is not free.
    for name, base, model, kind in (
            ("openai", "https://api.openai.com/v1", "gpt-4o", "openai"),
            ("deepseek", "https://api.deepseek.com", "deepseek-chat", "openai"),
            ("zai", "https://api.z.ai/api/anthropic", "GLM-5.3", "anthropic"),
            ("bigmodel", "https://open.bigmodel.cn/api/anthropic", "GLM-5.3", "anthropic"),
            ("openrouter", "https://openrouter.ai/api/v1", "z-ai/glm-5.3", "openai"),
            ("ollama", "http://127.0.0.1:11434/v1", "qwen2.5", "openai"),
    ):
        opted = bool(e.get(f"{name.upper()}_BASE_URL") or e.get(f"{name.upper()}_MODEL"))
        k = e.get(f"{name.upper()}_API_KEY") or ("ollama" if name == "ollama" and opted else "")
        if k:
            routes.append(Route(name=name, base_url=e.get(f"{name.upper()}_BASE_URL", base),
                                api_key=k, model=e.get(f"{name.upper()}_MODEL", model),
                                kind=kind, weight=50, note="env"))

    # 3. a local gateway you already run, consumed as an endpoint. Reading only
    #    its model-server URL + its api-key-as-opaque-string; NOT its account
    #    store, NOT driving its refresh/check-in.
    gw_url = e.get("LOCAL_GATEWAY_BASE_URL")
    gw_key = e.get("LOCAL_GATEWAY_KEY") or _read_key_file(e.get("LOCAL_GATEWAY_KEY_FILE", ""))
    if gw_url:
        routes.append(Route(
            name="local-gateway", base_url=_expand(gw_url), api_key=gw_key,
            model=e.get("LOCAL_GATEWAY_MODEL", ""), kind="openai", weight=100,
            note="local OpenAI-compatible gateway, consume-only"))

    return [r for r in routes if r.base_url]


def _host(url):
    try:
        return urllib.parse.urlparse(url).netloc or url
    except Exception:
        return url


def _read_key_file(path):
    path = _expand(path)
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return ""


def _resolve_key(spec, env):
    """api_key may be a literal, an env var name, or a file path. All are yours.
    No source here ever reads a browser/CLI credential store."""
    if "api_key" in spec:
        return _expand(spec["api_key"])
    if "api_key_env" in spec:
        return env.get(spec["api_key_env"], "")
    if "api_key_file" in spec:
        return _read_key_file(spec["api_key_file"])
    return ""
