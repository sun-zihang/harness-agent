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


GENERAL = textwrap.dedent(
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

    Finish the job with real executed work, then report what you did and what you
    found. Do not stop at describing a method you did not run.
    """
).strip()


# 数赛模式 is a COMPETENCE profile, not a restriction profile: it does not add or
# remove any approval gate (there is none in either mode). It fixes the deliverable
# to what actually scores points in CUMCM/美赛, because the generic prompt above
# lets a model stop at "here is how you would model it".
COMPETITION = textwrap.dedent(
    """\
    MODE: 数学建模竞赛（数赛模式）. Output prose in Chinese; code, identifiers and
    the JSON of any structured result stay as-is.

    Modelling discipline -- these are rules, not advice, because this is what the
    markers reward:
      - a number in the report must come from a block you actually ran. Never
        quote a figure you did not compute.
      - fix the trio first: 决策变量 / 单标量目标函数（带单位）/ 约束（每条注明
        它是资源、技术还是政策约束，以及为什么收紧）。
      - solve, THEN validate: substitute the answer back into every constraint and
        report the slack of each binding one.
      - cross-check with an independent path where one exists (two solvers, a
        coarser step, a closed form). Report disagreement, do not hide it.
      - never answer a sensitivity question by multiplying a shadow price by a
        proposed change -- use shadow_price_analysis, which re-solves and reports
        the range where the marginal actually holds.
      - perturb every parameter you assumed fixed by +-10% and say which
        conclusions survive. If nothing is sensitive, state that as a finding.
      - units must close; check them by dimensional inspection.

    Report skeleton for final_answer (omit a section only if genuinely not applicable,
    and say why):
      1. 问题重述  2. 模型假设（编号列出）  3. 符号说明
      4. 模型建立  5. 求解方法与算法  6. 数值结果（含单位、图表文件路径）
      7. 结果验证  8. 灵敏度分析  9. 模型优缺点与改进方向

    Refuse none of this by shortcut: a model more complex than the data, an R^2
    without residuals, or a truncated chart axis all cost marks.
    """
).strip()


MODES = {"general": GENERAL, "competition": GENERAL + "\n\n" + COMPETITION}


def system_prompt(mode="general"):
    """Explicit validation: a typo'd mode must fail loudly. Falling back to
    'general' silently would hand someone 数赛-mode-shaped output with none of the
    discipline, which is the decorative-switch failure this repo tests against."""
    try:
        return MODES[mode]
    except KeyError:
        raise ValueError(f"unknown mode {mode!r}; choose from "
                         f"{sorted(MODES)}") from None


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
                 out=None, resume_from=None, mode="general"):
        self.router = router
        self.kernel = Kernel(workspace=workspace, bootstrap=BOOTSTRAP)
        self.max_steps = max_steps
        self.on_event = on_event or (lambda *a: None)
        self.events = Events(out or "", resume=bool(resume_from))
        self.mode = mode                       # validated eagerly: a typo must die
        self.system = system_prompt(mode)      # here, not silently at step 40
        self.notes = []

    def _tool_text(self):
        return "\n".join(f"  {sig} -- {desc}" for sig, desc in tool_docs())

    def run(self, task):
        # `resume` only chooses the events filename (append vs a new timestamped
        # file); it does NOT re-inject prior turns into context. Continuing a
        # truncated run is a real feature; faking it here would be the decorative
        # kind of thing every other file in this repo tests against.
        messages = [
            {"role": "system", "content": self.system + "\n\nAvailable tools in scope:\n"
             + self._tool_text()},
            {"role": "user", "content": task},
        ]
        self.events.emit("meta", task=task, max_steps=self.max_steps, mode=self.mode)
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
