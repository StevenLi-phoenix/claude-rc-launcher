#!/usr/bin/env python3
"""Run the page against made-up projects and sessions, for UI work and screenshots.

No Ghostty, tmux or claude is touched: tmux is a stub that serves canned panes and `open` is
/usr/bin/true, so the launch button only creates a "launching" card.

usage: scripts/demo.py [port]   (default 8796), then open http://127.0.0.1:<port>/
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECTS = ["web-app", "data-pipeline", "ios-client", "blog", "infra", "ml-experiments", "dotfiles", "notes-sync"]
URL = "https://claude.ai/code/session_01DEMOdemoDEMOdemoDEMO{}"
CONVERSATIONS = [
    ("fix-flaky-checkout-test", "fix the flaky checkout test"),
    ("migrate-auth-to-passkeys", "move login to passkeys, keep password fallback for a release"),
    ("speed-up-ci-cache", "CI takes 14 minutes, find what to cache"),
    ("dark-mode-polish", "the settings page looks off in dark mode"),
]
PROMPT_BOX = """
────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle)
"""
PANES = {
    "web-app": f"""
❯ fix the flaky checkout test

  /remote-control is active · Continue here, on your phone, or at {URL.format(1)}

⏺ Reading tests/checkout.spec.ts…
⏺ Bash(pnpm test checkout --repeat 20)
  ⎿  19 passed, 1 failed: timeout waiting for #pay-button

✻ Investigating the race in useCart()… (esc to interrupt)
{PROMPT_BOX}""",
    "data-pipeline": f"""
  /remote-control is active · Continue here, on your phone, or at {URL.format(2)}
{PROMPT_BOX}
────────────────────────────────────────────────────────────────
  Teach auto mode about your environment?

  Auto mode works better when it knows your environment. Takes about a minute.

  ❯ 1. Yes
    2. Not now
    3. Don't show again

  Enter to confirm · Esc to cancel
""",
    "ios-client": f"""
❯ bump the deployment target to iOS 18 and fix warnings

  /remote-control is active · Continue here, on your phone, or at {URL.format(3)}

⏺ Done. Raised IPHONEOS_DEPLOYMENT_TARGET to 18.0 and removed 14 availability checks.
  The build is clean; 212 tests pass.

✻ Worked for 3m 12s
{PROMPT_BOX}""",
}

STUB = """#!/usr/bin/env python3
import json, sys
state = json.load(open({state!r}))
args = sys.argv[3:]  # drop -L <socket>
if args[0] == "list-sessions":
    for name, s in state.items():
        print(f"{{name}}\\t{{s['created']}}\\t0")
elif args[0] == "capture-pane":
    target = args[args.index("-t") + 1].strip("=:")
    sys.stdout.write(state.get(target, {{}}).get("pane", ""))
"""


def main() -> None:
    port = sys.argv[1] if len(sys.argv) > 1 else "8796"
    tmp = Path(tempfile.mkdtemp(prefix="rc-launcher-demo-"))
    roots = tmp / "Codes"
    for i, name in enumerate(PROJECTS):
        (roots / name).mkdir(parents=True)
        os.utime(roots / name, (time.time() - i * 3600, time.time() - i * 3600))
    now = time.time()
    created = {"web-app": now - 1260, "data-pipeline": now - 95, "ios-client": now - 7400}
    state = {f"rc-{n}": {"created": int(t), "pane": PANES[n]} for n, t in created.items()}
    (tmp / "tmux.json").write_text(json.dumps(state))
    stub = tmp / "tmux"
    stub.write_text(STUB.format(state=str(tmp / "tmux.json")))
    stub.chmod(0o755)
    for name, t in [*created.items(), ("blog", now - 4)]:
        sdir = tmp / "state" / "sessions" / name
        sdir.mkdir(parents=True)
        meta = {"name": name, "tmux": f"rc-{name}", "dir": str(roots / name), "mode": "auto", "continue": False,
                "prompt": True, "created": t, "seen": name != "blog", "rc_url": None, "failed": None}  # fmt: skip
        (sdir / "meta.json").write_text(json.dumps(meta))
    history = {str((roots / n).resolve()): now - i * 600 for i, n in enumerate(["web-app", "data-pipeline", "blog"])}
    # earlier conversations in web-app, for the resume picker
    tdir = tmp / "claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str((roots / "web-app").resolve()))
    tdir.mkdir(parents=True)
    for i, (title, ask) in enumerate(CONVERSATIONS):
        f = tdir / f"{i:08d}-0000-4000-8000-000000000000.jsonl"
        recs = [{"type": "user", "message": {"role": "user", "content": ask}}, {"type": "ai-title", "aiTitle": title},
                {"type": "last-prompt", "lastPrompt": ask}]  # fmt: skip
        f.write_text("".join(json.dumps(r) + "\n" for r in recs))
        os.utime(f, (now - 3600 * (i * 7 + 2),) * 2)
    (tmp / "state" / "history.json").write_text(json.dumps(history))
    os.environ |= {
        "RC_LAUNCHER_PORT": port,
        "RC_LAUNCHER_ROOTS": str(roots),
        "RC_LAUNCHER_STATE_DIR": str(tmp / "state"),
        "RC_LAUNCHER_TMUX": str(stub),
        "RC_LAUNCHER_CLAUDE": "/usr/bin/true",
        "RC_LAUNCHER_OPEN": "/usr/bin/true",
        "RC_LAUNCHER_HOST_LABEL": "studio",
        "RC_LAUNCHER_SCRATCH": str(tmp / "scratch"),
        "RC_LAUNCHER_CLAUDE_HOME": str(tmp / "claude"),
    }
    sys.path.insert(0, str(REPO))
    import rc_launcher

    print(f"demo data in {tmp}; open http://127.0.0.1:{port}/", file=sys.stderr)
    rc_launcher.main()


if __name__ == "__main__":
    main()
