"""browser_cursor — a visible, gliding cursor overlay for browser drives (#113, C1).

The founder's most visceral complaint: "I should be able to SEE the mouse
moving." On the signed-in headed Chrome (a real window on the user's screen)
our actions were invisible — clicks just happened. This injects a DOM cursor
that glides to each target before the click and pulses on contact, so a person
watching sees the agent work.

Design constraints, learned the hard way upstream:
- NO requestAnimationFrame loop. Cua's overlay pegged a CPU core (upstream
  issues #28152/#47032) and got disabled on macOS. This uses ONE css transition
  per move and self-removes when idle — zero steady-state cost.
- The injected script is a FIXED CONSTANT owned by us. It is never assembled
  from model input, and it must not travel the model-facing eval path — the
  caller passes it straight to the CLI `eval`, gate-exempt because it is ours,
  not the model's (the security seam in the gap analysis, Row 4).
- Shadow DOM + max z-index so it can't be styled away or collide with the page,
  and pointer-events:none so it never eats a real click.
"""
from __future__ import annotations

import json
from typing import Optional, Any, Dict

# One idempotent script: create-or-reuse a shadow-hosted cursor, then move it.
# {X}/{Y} are numeric substitutions we control; nothing else is interpolated.
_CURSOR_JS = """
(() => {
  const HOST_ID = '__lucaryin_cursor_host__';
  let host = document.getElementById(HOST_ID);
  if (!host) {
    host = document.createElement('div');
    host.id = HOST_ID;
    host.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;z-index:2147483647;pointer-events:none;';
    const root = host.attachShadow({mode:'open'});
    root.innerHTML = `<style>
      .c{position:fixed;width:22px;height:22px;margin:-4px 0 0 -4px;pointer-events:none;
         transition:left .28s cubic-bezier(.22,1,.36,1),top .28s cubic-bezier(.22,1,.36,1);
         will-change:left,top;filter:drop-shadow(0 1px 2px rgba(0,0,0,.45));}
      .c svg{display:block}
      .p{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;
         border:2px solid rgba(20,184,166,.9);opacity:0;pointer-events:none;}
      .p.go{animation:lucpulse .5s ease-out}
      @keyframes lucpulse{0%{opacity:.9;transform:scale(.3)}100%{opacity:0;transform:scale(1.3)}}
    </style>
    <div class="c" id="luc-c"><svg viewBox="0 0 24 24" width="22" height="22">
      <path d="M4 2l6 15 2.3-6.2L18.5 8.5z" fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg></div>
    <div class="p" id="luc-p"></div>`;
    (document.body||document.documentElement).appendChild(host);
  }
  const root = host.shadowRoot;
  const cur = root.getElementById('luc-c');
  const pul = root.getElementById('luc-p');
  const x = {X}, y = {Y};
  cur.style.left = x + 'px'; cur.style.top = y + 'px';
  if ({PULSE}) { pul.style.left = x+'px'; pul.style.top = y+'px';
    pul.classList.remove('go'); void pul.offsetWidth; pul.classList.add('go'); }
  clearTimeout(window.__lucCursorTimer);
  window.__lucCursorTimer = setTimeout(() => { const h=document.getElementById(HOST_ID); if(h) h.remove(); }, 8000);
  return 'ok';
})()
""".strip()


def cursor_move_js(x: float, y: float, pulse: bool = False) -> str:
    """Build the exact eval string for a cursor move. Only numeric x/y and a
    bool are substituted — never any caller/model string."""
    return (_CURSOR_JS
            .replace("{X}", str(int(x)))
            .replace("{Y}", str(int(y)))
            .replace("{PULSE}", "true" if pulse else "false"))


def box_center(box: Dict[str, Any]) -> Optional[tuple]:
    """Center point of a get-box result, or None if it isn't a usable box."""
    try:
        x = float(box["x"]); y = float(box["y"])
        w = float(box.get("width", 0)); h = float(box.get("height", 0))
        return (x + w / 2.0, y + h / 2.0)
    except (KeyError, TypeError, ValueError):
        return None


def show_cursor_at(run_cmd, task_id: str, ref: str, pulse: bool = False) -> bool:
    """Best-effort: resolve ref -> box -> glide the cursor to its center.

    `run_cmd` is browser_tool._run_browser_command (injected to avoid a cycle).
    Returns True if the cursor moved. NEVER raises and NEVER blocks the real
    action — a cursor that fails is invisible, not broken. Only meaningful on a
    headed/visible browser; on headless it's a harmless no-op eval.
    """
    try:
        if not ref.startswith("@"):
            ref = f"@{ref}"
        r = run_cmd(task_id, "get", ["box", ref], timeout=8)
        if not r.get("success"):
            return False
        data = r.get("data", r)
        box = data.get("box", data) if isinstance(data, dict) else data
        center = box_center(box) if isinstance(box, dict) else None
        if not center:
            return False
        run_cmd(task_id, "eval", [cursor_move_js(center[0], center[1], pulse=pulse)], timeout=8)
        return True
    except Exception:
        return False
