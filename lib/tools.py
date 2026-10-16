"""Tools injected into the kernel namespace, and the bootstrap that installs them.

Design constraint that shapes this file: the kernel runs in a SEPARATE process,
so a tool cannot be a Python object passed over the wire -- it must be SOURCE
text the child exec's. Hence BOOTSTRAP is code, and every helper here has to be
defined here or imported from a module the child can see on sys.path.

Two families:
  * harness primitives -- final_answer (stop), notes (collect for the report)
  * modelling tools    -- thin re-export of mathmodel's verified engine, so the
    standalone agent and the plugin agree numerically rather than me rewriting
    and risking a divergent solver. Reuse over reinvention, the whole point.
"""
from __future__ import annotations

import os

# mathmodel/engine holds tools_modelling.py (35 passing assertions, dual-solver
# cross-check). The standalone agent imports it rather than copying it, so a fix
# to one is a fix to both and the two cannot drift apart silently.
MATHMODEL_ENGINE = os.environ.get("MATHMODEL_ENGINE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "mathmodel", "engine")

BOOTSTRAP = f'''
import sys, os as _os
_ENGINE = {MATHMODEL_ENGINE!r}
if _os.path.isdir(_ENGINE) and _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

# -- harness primitives ---------------------------------------------------
def final_answer(text):
    """STOP. Call this as the LAST line with your finished answer."""
    raise FinalAnswer(text)

_NOTES = globals().setdefault("__notes__", [])
def note(text):
    """Record a finding you want to appear in the report; returns the count."""
    _NOTES.append(text); return len(_NOTES)

# -- modelling engine (shared with the plugin) ----------------------------
try:
    import tools_modelling as tm
    from tools_modelling import (
        solve_lp, solve_lp_max, shadow_price_analysis, sensitivity,
        newton, bisect, rk4, describe, ols, pearson, table, plot,
        ascii_plot, save)
    _HAVE_TM = True
except Exception as _e:                      # absent engine is not fatal
    _HAVE_TM = False
    _TM_ERR = str(_e)

def kernel_info():
    return {{"modelling_engine": bool(_HAVE_TM),
             "engine_path": _ENGINE if _HAVE_TM else _TM_ERR}}
'''


def tool_docs():
    """Compact, model-facing catalogue. Kept to plain strings (not introspection)
    so it is stable regardless of whether the optional engine loaded -- the model
    must never be told a function exists that raised on import."""
    base = [
        ("final_answer(text)", "call LAST to stop and return your report"),
        ("note(text)", "stash a finding for the report"),
        ("kernel_info()", "is the modelling engine loaded? (dict)"),
        ("open(path,'w').write(...) / print(...)", "normal python: files, stdout"),
    ]
    tm = [
        ("solve_lp_max(profit,A,b)", "maximise a linear objective; returns x, max_objective, marginals"),
        ("shadow_price_analysis(profit,A,b,row,delta)", "REAL gain vs the naive marginal*delta, plus valid range"),
        ("sensitivity(fn,param)", "+-10% elasticities table"),
        ("rk4(f,y0,t0,t1,n)", "integrate an ODE system"),
        ("newton(f,df,x0) / bisect(f,a,b)", "root finding"),
        ("describe(xs) / ols(xs,ys) / pearson(xs,ys)", "stats with the residuals/CIs a report needs"),
        ("plot(xs,ys,path) / ascii_plot(...)", "PNG if matplotlib, else ASCII; never silent"),
        ("table(rows,headers)", "markdown-ish aligned table for the write-up"),
    ]
    return base + tm
