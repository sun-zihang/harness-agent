"""CLI: python run.py "task" [--model NAME] [--config cfg.json]

The operator's instruction is the only authority. No confirmation prompt -- the
loop runs to completion or to --max-steps, and prints the report.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))

from agent import Agent, MODES          # noqa: E402
from providers import Router, load_routes  # noqa: E402


def do_check(env):
    routes = load_routes(env=env)
    if not routes:
        print("no routes. Point the agent at any OpenAI/Anthropic-compatible\n"
              "endpoint via config endpoints[] or env keys (OPENAI_API_KEY, ...)\n"
              "or a running LOCAL_GATEWAY_BASE_URL. See README.")
        return 1
    print(f"{len(routes)} route(s):")
    for r in routes:
        alive = r.probe()
        print(f"  [{'live' if alive else 'down':4}] {r.name:14} {r.base_url}  model={r.model}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", help="instruction, or @path to read")
    ap.add_argument("--model", help="prefer this route name or model id")
    ap.add_argument("--config", help="JSON with an endpoints[] list")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--mode", choices=sorted(MODES), default="general",
                    help="general = 通用执行体; competition = 数赛模式（中文报告、"
                         "假设编号、求解后验证、灵敏度用 shadow_price_analysis 而非"
                         "乘增量）。两者都没有审批步骤——这个开关切换的是产出纪律，"
                         "不是限制强度。")
    ap.add_argument("--out", default="out/report.md")
    ap.add_argument("--events", default="out/events.jsonl")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--check", action="store_true", help="list routes, exit")
    a = ap.parse_args(argv)

    env = os.environ
    cfg = {}
    if a.config and os.path.exists(a.config):
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)

    if a.check:
        return do_check(env)
    if not a.task:
        ap.error("need a task (or --check)")

    task = a.task
    if task.startswith("@"):
        with open(task[1:], encoding="utf-8") as fh:
            task = fh.read()

    routes = load_routes(cfg=cfg, env=env)
    if not routes:
        print("no routes configured; run: python run.py --check", file=sys.stderr)
        return 2
    router = Router(routes, ledger_path=os.path.join(
        os.path.dirname(os.path.abspath(a.out)) or ".", "usage.json"))

    def ev(kind, step, txt):
        if not a.quiet:
            print(f"[{kind} {step}] {str(txt)[:160]}", flush=True)

    agent = Agent(router, max_steps=a.max_steps, on_event=ev, out=a.events,
                  mode=a.mode)
    report = agent.run(task)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"\n=== {a.out} ===\n{report}")
    if not a.quiet:
        print(f"\n[events] {agent.events.path}")
        print(f"[usage ] {json.dumps(router.summary(), ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
