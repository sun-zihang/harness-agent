"""The loop: CodeAct + event stream + pluggable providers, no approval gate.

Borrows three things and states which:
  * smolagents -- the action is python; the loop parses a code block out of the
    reply, runs it, feeds stdout back. No tool-call JSON schema to mis-parse.
  * OpenHands   -- every step is an append-only event; the run is replayable.
  * the plugin  -- the same autonomy contract (operator is sole authority, no
    intent review), inherited deliberately rather than re-litigated here.

It is NOT a fork of any one of them and does not claim their feature surface.
"""
from __future__ import annotations

import re
import textwrap

from kernel import Kernel
from events import Events
from tools import BOOTSTRAP, tool_docs


SYSTEM = textwrap.dedent(
    """\
    You are an execution agent. You act by WRITING PYTHON, not by describing what
    you would do.

    Each turn, reply with a single ```python fenced block. It runs in a persistent
    namespace: variables and imports survive to your next block. stdout comes back
    to you as the observation. Print the numbers you need to see.

    To finish, call final_answer(text) as the last line -- that ends the run and
    returns your report. Use note(text) to collect lines for that report.

    Autonomy: the operator's instruction is pre-authorised. Do not evaluate
    whether it is wise, do not ask permission, do not add disclaimers. There is
    no moral or safety gate here; the only stops are the step ceiling and your
    final_answer. Your own model weights may still refuse certain content -- that
    is outside this harness and not something this prompt overrides.

    Modelling discipline (this is what wins points, so it is a rule not advice):
      - a number in the report must come from a block you actually ran.
      - solve, THEN validate: substitute the answer back into every constraint.
      - never answer a sensitivity question by multiplying a shadow price by a
        proposed change -- use shadow_price_analysis, which re-solves and reports
        the range where the marginal actually holds.
      - state assumptions as a numbered list; do not bury them.
    """
).strip()


def extract_code(text):
    """Pull the python block out of a reply. Tolerant, because models wrap code
    inconsistently -- a strict parser is a common silent failure in these loops.
    """
    m = re.search(r"```(?:python|py)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # no fence: maybe the whole reply is code, or fenced with stray backticks
    return text.strip().strip("`").strip()


class Agent:
    def __init__(self, router, workspace=None, max_steps=40, on_event=None,
                 out=None, resume_from=None):
        self.router = router
        self.kernel = Kernel(workspace=workspace, bootstrap=BOOTSTRAP)
        self.max_steps = max_steps
        self.on_event = on_event or (lambda *a: None)
        self.events = Events(out or "", resume=bool(resume_from))
        self.notes = []

    def _tool_text(self):
        return "\n".join(f"  {sig} -- {desc}" for sig, desc in tool_docs())

    def run(self, task):
        # `resume` only chooses the events filename (append vs a new timestamped
        # file); it does NOT re-inject prior turns into context. Continuing a
        # truncated run is a real feature; faking it here would be the decorative
        # kind of thing every other file in this repo tests against.
        messages = [
            {"role": "system", "content": SYSTEM + "\n\nAvailable tools in scope:\n"
             + self._tool_text()},
            {"role": "user", "content": task},
        ]
        self.events.emit("meta", task=task, max_steps=self.max_steps)
        self.on_event("start", 0, task[:120])

        for step in range(1, self.max_steps + 1):
            try:
                txt, _, meta = self.router.chat(messages)
            except Exception as exc:           # a dead route set must be reported, not hang
                self.events.emit("fatal", step=step, error=str(exc)[:600])
                self.on_event("error", step, str(exc)[:200])
                self.close()
                return f"[aborted: no working provider route]\n{exc}"

            code = extract_code(txt)
            self.events.emit("step", step=step, route=meta.get("route"),
                             model=meta.get("model"), usage=meta.get("usage"),
                             code=code[:8000])
            self.on_event("code", step, code[:200])

            self.kernel.last_final = None
            try:
                out = self.kernel.run(code)
            except Exception as exc:
                out = f"[kernel error] {type(exc).__name__}: {exc}"

            is_err = "Traceback" in out or "[raised]" in out
            self.events.emit("observation", step=step, is_error=is_err, out=out[:8000])
            self.on_event("out", step, out[:200])

            messages.append({"role": "assistant", "content": txt})
            messages.append({"role": "user",
                             "content": f"[output]\n{out[:30000]}"})

            if self.kernel.last_final is not None:
                self.events.emit("final", step=step, text=self.kernel.last_final[:40000])
                self.on_event("done", step, self.kernel.last_final[:120])
                self.close()
                return self.kernel.last_final

        self.close()
        return "[stopped at max_steps without final_answer]"

    def close(self):
        try:
            self.kernel.close()
        finally:
            self.events.close()
