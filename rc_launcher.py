#!/usr/bin/env python3
"""claude-rc-launcher: start a Claude Code session with Remote Control on this Mac, from any device.

Each launch opens a new Ghostty window, which attaches to a tmux session on a dedicated socket,
which runs `claude --remote-control`. Ghostty is the point: it runs inside the GUI login session,
so the session inherits Ghostty's privacy (TCC) grants and the unlocked login keychain, neither of
which an SSH-started process gets. The tmux server is always born inside a Ghostty window; this
daemon only ever runs client commands against it (list / capture / send-keys / kill), so every
session keeps Ghostty's TCC context while still being observable and controllable from here.

Stdlib only. Configuration comes from RC_LAUNCHER_* environment variables (see Config.from_env).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

log = logging.getLogger("rc-launcher")

MODES = ("auto", "acceptEdits", "plan", "bypassPermissions", "manual")
SESSION_PREFIX = "rc-"
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
RC_URL_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
EXIT_RE = re.compile(r"\[rc-launcher\] claude exited \((\d+)\)")
LOGIN_RE = re.compile(r"Select login method|Please run /login|Invalid API key|OAuth token has expired")
PROMPT_RE = re.compile(r"⏵⏵|shift\+tab to cycle|\? for shortcuts|esc to interrupt")
SEPARATOR_RE = re.compile(r"^\s*[─━]{8,}\s*$")
SELECTED_RE = re.compile(r"^(\s*)❯\s(.*\S)\s*$")
OPTION_RE = re.compile(r"^(\s*)(\S.*?)\s*$")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}
CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class BadRequest(ValueError):
    """The request is malformed or asks for something not allowed (HTTP 400)."""


class NotFound(LookupError):
    """No such session (HTTP 404)."""


class LaunchError(RuntimeError):
    """Ghostty could not be opened (HTTP 502)."""


# --------------------------------------------------------------------------- configuration


def _default_session_path() -> str:
    home = Path.home()
    return f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _split(value: str, sep: str) -> list[str]:
    return [v.strip() for v in value.split(sep) if v.strip()]


@dataclass(frozen=True)
class Config:
    port: int = 8795
    bind: str = "127.0.0.1"
    roots: tuple[Path, ...] = (Path.home() / "Codes",)
    state_dir: Path = Path.home() / ".claude-rc-launcher"
    # Tailscale identities allowed in (Tailscale-User-Login header set by `tailscale serve`). Empty = no check.
    allowed_logins: frozenset[str] = frozenset()
    # Host names this daemon is reached under (besides localhost); requests for any other Host are refused.
    public_hosts: frozenset[str] = frozenset()
    ghostty_app: str = "/Applications/Ghostty.app"
    open_bin: str = "/usr/bin/open"
    tmux_bin: str = "tmux"
    claude_bin: str = "claude"
    socket: str = "claude-rc"
    default_mode: str = "auto"
    session_path: str = field(default_factory=_default_session_path)
    host_label: str = ""
    launch_timeout: float = 45.0
    watch_seconds: float = 180.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        base = cls()
        path = env.get("RC_LAUNCHER_SESSION_PATH", base.session_path)

        def which(name: str, override: str) -> str:
            return env.get(override) or shutil.which(name, path=path) or name

        mode = env.get("RC_LAUNCHER_DEFAULT_MODE", base.default_mode)
        if mode not in MODES:
            raise SystemExit(f"RC_LAUNCHER_DEFAULT_MODE must be one of {MODES}")
        roots = env.get("RC_LAUNCHER_ROOTS")
        return cls(
            port=int(env.get("RC_LAUNCHER_PORT", base.port)),
            bind=env.get("RC_LAUNCHER_BIND", base.bind),
            roots=tuple(Path(os.path.expanduser(r)) for r in _split(roots, ":")) if roots else base.roots,
            state_dir=Path(os.path.expanduser(env.get("RC_LAUNCHER_STATE_DIR", str(base.state_dir)))),
            allowed_logins=frozenset(_split(env.get("RC_LAUNCHER_ALLOWED_LOGINS", ""), ",")),
            public_hosts=frozenset(h.lower() for h in _split(env.get("RC_LAUNCHER_PUBLIC_HOSTS", ""), ",")),
            ghostty_app=env.get("RC_LAUNCHER_GHOSTTY_APP", base.ghostty_app),
            tmux_bin=which("tmux", "RC_LAUNCHER_TMUX"),
            claude_bin=which("claude", "RC_LAUNCHER_CLAUDE"),
            socket=env.get("RC_LAUNCHER_TMUX_SOCKET", base.socket),
            default_mode=mode,
            session_path=path,
            host_label=env.get("RC_LAUNCHER_HOST_LABEL") or os.uname().nodename.split(".")[0],
        )


# --------------------------------------------------------------------------- names & paths


def sanitize_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    name = re.sub(r"-{2,}", "-", name)[:40].strip("-")
    return name or "session"


def allowed_bases(cfg: Config) -> list[Path]:
    """Roots plus the targets of symlinked root children (e.g. ~/Codes/x -> /Volumes/SSD/x)."""
    bases: list[Path] = []
    for root in cfg.roots:
        if not root.is_dir():
            continue
        bases.append(root.resolve())
        for child in root.iterdir():
            if child.is_symlink() and child.is_dir():
                bases.append(child.resolve())
    return bases


def resolve_project(cfg: Config, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise BadRequest("dir is required")
    path = Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        raise BadRequest("dir must be an absolute path")
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise BadRequest(f"{raw} does not exist") from exc
    if not real.is_dir():
        raise BadRequest(f"{raw} is not a directory")
    if not any(real == b or b in real.parents for b in allowed_bases(cfg)):
        raise BadRequest(f"{raw} is outside the allowed roots")
    return real


# --------------------------------------------------------------------------- launch commands


def ghostty_argv(cfg: Config, tmux_name: str, workdir: Path, script: Path, title: str) -> list[str]:
    """`open -n` starts a separate Ghostty instance per session: no Apple Events, so no TCC prompt
    for this daemon, and the instance quits once its window closes (the tmux session lives on)."""
    return [
        cfg.open_bin, "-na", cfg.ghostty_app, "--args",
        f"--title=claude-rc · {title}",
        "--quit-after-last-window-closed=true",
        "--window-save-state=never",
        "--confirm-close-surface=false",
        "--abnormal-command-exit-runtime=0",  # with the line above: a fast exit still closes the instance
        "--auto-update=off",  # otherwise Sparkle's update prompt keeps the instance alive after the window closes
        "-e", cfg.tmux_bin, "-L", cfg.socket, "new-session", "-A", "-s", tmux_name,
        "-c", str(workdir), str(script),
    ]  # fmt: skip


def render_run_script(
    cfg: Config, *, workdir: Path, name: str, mode: str, cont: bool, prompt_file: Path, keep_shell: bool = True
) -> str:
    q = shlex.quote
    lines = [
        "#!/bin/bash",
        f"# Written by claude-rc-launcher for session {name}; runs inside tmux inside Ghostty.",
        "set -uo pipefail",
        f"export PATH={q(cfg.session_path)}",
        'export LANG="${LANG:-en_US.UTF-8}"',
        "unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT NO_COLOR",
        f"cd {q(str(workdir))} || exit 1",
        f"args=(--remote-control={q(name)} --permission-mode {q(mode)})",
    ]
    if cont:
        lines.append("args+=(--continue)")
    lines += [
        f"prompt={q(str(prompt_file))}",
        'if [ -s "$prompt" ]; then args+=("$(cat "$prompt")"); fi',
        # caffeinate holds a no-sleep assertion for exactly as long as claude runs
        f'caffeinate -ims {q(cfg.claude_bin)} "${{args[@]}}"',
        "code=$?",
        'echo "[rc-launcher] claude exited ($code)"',
    ]
    if keep_shell:  # keep the pane so the exit reason stays readable; Stop/Dismiss kills it
        lines.append("exec /bin/zsh -l")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- pane parsing


@dataclass(frozen=True)
class Dialog:
    title: str
    body: str
    options: list[str]
    selected: int


@dataclass(frozen=True)
class PaneState:
    state: str  # starting | running | needs-input | needs-login | exited
    busy: bool = False
    rc_url: str | None = None
    dialog: Dialog | None = None
    exit_code: int | None = None
    tail: list[str] = field(default_factory=list)


def _option(line: str) -> tuple[int, str, bool]:
    """(text indent, text, selected) for a menu line."""
    m = SELECTED_RE.match(line)
    if m:
        return len(m.group(1)) + 2, m.group(2), True
    m = OPTION_RE.match(line)
    assert m is not None
    return len(m.group(1)), m.group(2), False


def parse_dialog(lines: list[str]) -> Dialog | None:
    """A blocking select menu ends with 'Enter to confirm'; its options sit right above, one marked ❯."""
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    confirm = next((i for i in reversed(nonblank[-3:]) if "Enter to confirm" in lines[i]), None)
    if confirm is None:
        return None
    options: list[tuple[str, bool]] = []
    indent: int | None = None
    top = confirm
    for j in range(confirm - 1, -1, -1):
        line = lines[j]
        if not line.strip():
            if options:
                break
            continue
        ind, text, sel = _option(line)
        if indent is None:
            indent = ind
        elif ind != indent:
            break
        options.insert(0, (text, sel))
        top = j
    selected = [i for i, (_, sel) in enumerate(options) if sel]
    if len(selected) != 1:
        return None
    head: list[str] = []
    for j in range(top - 1, -1, -1):
        if SEPARATOR_RE.match(lines[j]):
            break
        if lines[j].strip():
            head.insert(0, lines[j].strip())
    return Dialog(
        title=head[0] if head else "",
        body=" ".join(head[1:])[:600],
        options=[t for t, _ in options],
        selected=selected[0],
    )


def parse_pane(text: str) -> PaneState:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    urls = RC_URL_RE.findall(text)
    rc_url = urls[-1] if urls else None
    bottom = "\n".join(lines[-15:])
    busy = "esc to interrupt" in bottom
    tail = [line for line in lines[-24:] if not SEPARATOR_RE.match(line)]
    exited = EXIT_RE.search(text)
    if exited:
        return PaneState("exited", rc_url=rc_url, exit_code=int(exited.group(1)), tail=tail)
    dialog = parse_dialog(lines)
    if dialog:
        return PaneState("needs-input", busy=busy, rc_url=rc_url, dialog=dialog, tail=tail)
    if LOGIN_RE.search(bottom):
        return PaneState("needs-login", rc_url=rc_url, tail=tail)
    if rc_url or PROMPT_RE.search(bottom):
        return PaneState("running", busy=busy, rc_url=rc_url, tail=tail)
    return PaneState("starting", tail=tail)


def parse_etime(text: str) -> int:
    """`ps -o etime` ([[dd-]hh:]mm:ss) in seconds."""
    days, _, clock = text.strip().rpartition("-")
    parts = [int(p) for p in clock.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return (int(days) if days else 0) * 86400 + hours * 3600 + minutes * 60 + seconds


def keys_for_option(selected: int, target: int) -> list[str]:
    step = "Down" if target > selected else "Up"
    return [step] * abs(target - selected) + ["Enter"]


# --------------------------------------------------------------------------- tmux client


def run_cmd(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=15, check=False)


class Tmux:
    """Client-side commands against the claude-rc tmux server. Never starts a server."""

    def __init__(self, cfg: Config, run: Runner) -> None:
        self.cfg = cfg
        self.run = run

    def _tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run([self.cfg.tmux_bin, "-L", self.cfg.socket, *args])

    def sessions(self) -> dict[str, dict[str, Any]]:
        p = self._tmux("list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_attached}")
        if p.returncode != 0:  # "no server running" is the normal empty state
            return {}
        out: dict[str, dict[str, Any]] = {}
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[1].isdigit():
                out[parts[0]] = {"created": int(parts[1]), "attached": parts[2] not in ("", "0")}
        return out

    def capture(self, name: str, history: int = 300) -> str:
        return self._tmux("capture-pane", "-p", "-J", "-S", f"-{history}", "-t", f"={name}:").stdout

    def send_keys(self, name: str, keys: list[str]) -> None:
        self._tmux("send-keys", "-t", f"={name}:", *keys)

    def kill(self, name: str) -> None:
        self._tmux("kill-session", "-t", f"={name}")


# --------------------------------------------------------------------------- state on disk


class History:
    """Last launch time per project dir, to put recently used projects first."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict[str, float]:
        try:
            data = json.loads(self.path.read_text())
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError):
            return {}

    def touch(self, project: str) -> None:
        with self.lock:
            data = self.load()
            data[project] = time.time()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1))
            tmp.replace(self.path)


# --------------------------------------------------------------------------- the launcher


class RCLauncher:
    def __init__(self, cfg: Config, run: Runner = run_cmd, watch: bool = True) -> None:
        self.cfg = cfg
        self.tmux = Tmux(cfg, run)
        self.run = run
        self.watch = watch
        self.clock: Callable[[], float] = time.time
        self.sessions_dir = cfg.state_dir / "sessions"
        self.history = History(cfg.state_dir / "history.json")
        self.lock = threading.RLock()
        self.claude_version = ""
        self.kill: Callable[[int, int], None] = os.kill

    # ---- metadata

    def _meta_path(self, name: str) -> Path:
        return self.sessions_dir / name / "meta.json"

    def _load(self, name: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._meta_path(name).read_text())
        except (OSError, ValueError):
            return None

    def _save(self, meta: dict[str, Any]) -> None:
        path = self._meta_path(meta["name"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, indent=1))
        tmp.replace(path)

    def _update(self, name: str, **changes: Any) -> None:
        with self.lock:
            meta = self._load(name)
            if meta is not None and any(meta.get(k) != v for k, v in changes.items()):
                meta.update(changes)
                self._save(meta)

    def _metas(self) -> list[dict[str, Any]]:
        if not self.sessions_dir.is_dir():
            return []
        metas = (self._load(d.name) for d in self.sessions_dir.iterdir() if NAME_RE.match(d.name))
        return [m for m in metas if m]

    # ---- projects

    def projects(self) -> list[dict[str, Any]]:
        used = self.history.load()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for root in self.cfg.roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if child.name.startswith(".") or not child.is_dir():
                    continue
                real = str(child.resolve())
                if real in seen:
                    continue
                seen.add(real)
                out.append(
                    {"name": child.name, "path": real, "last_used": used.get(real, 0.0),
                     "mtime": child.stat().st_mtime, "git": (child / ".git").exists()}
                )  # fmt: skip
        for path, ts in used.items():  # custom paths launched before, if still allowed
            if path not in seen:
                try:
                    real = str(resolve_project(self.cfg, path))
                except BadRequest:
                    continue
                seen.add(real)
                out.append({"name": Path(real).name, "path": real, "last_used": ts, "mtime": 0.0, "git": False})
        out.sort(key=lambda p: (-p["last_used"], -p["mtime"]))
        return out

    # ---- start

    def _unique_name(self, base: str, live: dict[str, Any]) -> str:
        now = self.clock()
        for n in range(1, 100):
            name = base if n == 1 else f"{base[:37]}-{n}"
            meta = self._load(name)
            pending = meta is not None and not meta.get("seen") and now - meta["created"] < self.cfg.launch_timeout
            if SESSION_PREFIX + name not in live and not pending:
                return name
        raise BadRequest("too many sessions with this name")

    def start(self, req: Mapping[str, Any]) -> dict[str, Any]:
        workdir = resolve_project(self.cfg, req.get("dir"))
        mode = req.get("mode") or self.cfg.default_mode
        if mode not in MODES:
            raise BadRequest(f"mode must be one of {', '.join(MODES)}")
        prompt = req.get("prompt") or ""
        if not isinstance(prompt, str) or len(prompt) > 20000:
            raise BadRequest("prompt must be a string under 20k characters")
        cont = bool(req.get("continue"))
        raw_name = req.get("name") if isinstance(req.get("name"), str) and req.get("name") else workdir.name
        with self.lock:
            name = self._unique_name(sanitize_name(raw_name), self.tmux.sessions())
            sdir = self.sessions_dir / name
            if sdir.exists():  # leftovers of an ended session with the same name
                shutil.rmtree(sdir)
            sdir.mkdir(parents=True)
            prompt_file = sdir / "prompt.md"
            prompt_file.write_text(prompt)
            script = sdir / "run.sh"
            script.write_text(
                render_run_script(self.cfg, workdir=workdir, name=name, mode=mode, cont=cont, prompt_file=prompt_file)
            )
            script.chmod(0o700)
            meta = {
                "name": name, "tmux": SESSION_PREFIX + name, "dir": str(workdir), "mode": mode,
                "continue": cont, "prompt": bool(prompt), "created": self.clock(), "seen": False,
                "rc_url": None, "failed": None,
            }  # fmt: skip
            self._save(meta)
        argv = ghostty_argv(self.cfg, meta["tmux"], workdir, script, name)
        log.info("launch %s in %s mode=%s continue=%s: %s", name, workdir, mode, cont, shlex.join(argv))
        p = self.run(argv)
        if p.returncode != 0:
            shutil.rmtree(sdir, ignore_errors=True)
            log.error("open failed for %s (rc=%s): %s", name, p.returncode, p.stderr.strip())
            raise LaunchError(f"could not open Ghostty: {p.stderr.strip() or p.returncode}")
        self.history.touch(str(workdir))
        if self.watch:
            threading.Thread(target=self._watch, args=(name,), name=f"watch-{name}", daemon=True).start()
        return meta

    # ---- watching a fresh launch: answer the trust dialog once, record the RC link

    def _watch(self, name: str) -> None:
        flags: dict[str, bool] = {}
        deadline = self.clock() + self.cfg.watch_seconds
        try:
            while self.clock() < deadline:
                time.sleep(1.5)
                if self.watch_step(name, flags):
                    return
            log.warning("watch %s: gave up after %ss without an RC link", name, self.cfg.watch_seconds)
        except Exception:
            log.exception("watch %s crashed", name)

    def watch_step(self, name: str, flags: dict[str, bool]) -> bool:
        """One poll of a launching session. Returns True when there is nothing left to watch."""
        meta = self._load(name)
        if meta is None:
            return True
        if meta["tmux"] not in self.tmux.sessions():
            if meta.get("seen"):
                return True
            if self.clock() - meta["created"] > self.cfg.launch_timeout:
                reason = "Ghostty never started the session; check the Mac's screen for a blocking dialog"
                log.error("watch %s: %s", name, reason)
                self._update(name, failed=reason)
                return True
            return False
        self._update(name, seen=True)
        pane = parse_pane(self.tmux.capture(meta["tmux"]))
        if pane.rc_url:
            self._update(name, rc_url=pane.rc_url)
        if pane.dialog:
            opts = pane.dialog.options
            for flag, wanted, allowed in (
                ("trust", "Yes, I trust this folder", True),
                ("bypass", "Yes, I accept", meta["mode"] == "bypassPermissions"),
            ):
                hit = next((i for i, o in enumerate(opts) if o.startswith(wanted)), None)
                if hit is not None and allowed and not flags.get(flag):
                    log.info("watch %s: answering '%s'", name, wanted)
                    self.tmux.send_keys(meta["tmux"], keys_for_option(pane.dialog.selected, hit))
                    flags[flag] = True
            return False
        return pane.state == "exited" or (pane.rc_url is not None and pane.state == "running")

    # ---- listing

    def _view(self, meta: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
        view: dict[str, Any] = {
            "name": meta["name"], "dir": meta.get("dir"), "mode": meta.get("mode"),
            "created": meta.get("created"), "rc_url": meta.get("rc_url"), "busy": False,
            "dialog": None, "tail": [], "exit_code": None, "error": meta.get("failed"),
            "attach": f"{self.cfg.tmux_bin} -L {self.cfg.socket} attach -t {meta['tmux']}",
        }  # fmt: skip
        if meta["tmux"] in live:
            pane = parse_pane(self.tmux.capture(meta["tmux"]))
            if not meta.get("seen") or (pane.rc_url and pane.rc_url != meta.get("rc_url")):
                self._update(meta["name"], seen=True, rc_url=pane.rc_url or meta.get("rc_url"))
            view.update(
                state=pane.state, busy=pane.busy, rc_url=pane.rc_url or meta.get("rc_url"),
                dialog=asdict(pane.dialog) if pane.dialog else None, tail=pane.tail, exit_code=pane.exit_code,
            )  # fmt: skip
        elif meta.get("seen"):
            view["state"] = "ended"
        elif meta.get("failed") or self.clock() - meta["created"] > self.cfg.launch_timeout:
            view["state"] = "failed"
            view["error"] = meta.get("failed") or "Ghostty never started the session"
        else:
            view["state"] = "launching"
        return view

    def _ghostty_instances(self) -> list[tuple[int, int, str]]:
        """(pid, age in seconds, tmux session) of the Ghostty instances this launcher opened."""
        exe = f"{self.cfg.ghostty_app}/Contents/MacOS/ghostty "
        sig = re.compile(
            rf" -L {re.escape(self.cfg.socket)} new-session -A -s ({re.escape(SESSION_PREFIX)}[A-Za-z0-9_-]+) "
        )
        found = []
        for line in self.run(["/bin/ps", "-axww", "-o", "pid=,etime=,args="]).stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[2].startswith(exe):
                m = sig.search(parts[2])
                if m:
                    found.append((int(parts[0]), parse_etime(parts[1]), m.group(1)))
        return found

    def reap(self, live: Mapping[str, Any]) -> None:
        """Quit Ghostty instances whose tmux session is gone. Ghostty normally quits by itself once its
        window closes, but a leftover panel can keep an instance alive, which would pile up one per launch.
        Only instances older than the launch timeout are touched, so a session still starting is safe. A name
        can be reused after a session ends, so an instance much older than the live session of that name
        belongs to the earlier one."""
        now = self.clock()
        for pid, age, tname in self._ghostty_instances():
            session = live.get(tname)
            stale = session is None or now - age < session["created"] - 10
            if stale and age > self.cfg.launch_timeout:
                log.info("reap Ghostty pid %s: its session %s is gone", pid, tname)
                try:
                    self.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def list_sessions(self) -> list[dict[str, Any]]:
        live = self.tmux.sessions()
        self.reap(live)
        metas = self._metas()
        known = {m["tmux"] for m in metas}
        for tname, info in live.items():  # sessions whose metadata is gone (e.g. state dir wiped)
            if tname.startswith(SESSION_PREFIX) and tname not in known:
                metas.append(
                    {"name": tname[len(SESSION_PREFIX) :], "tmux": tname, "created": info["created"], "seen": True}
                )
        views = [self._view(m, live) for m in metas]
        views.sort(key=lambda v: -(v.get("created") or 0))
        return views

    # ---- actions on a session

    def _require(self, name: str) -> tuple[dict[str, Any] | None, str, bool]:
        if not NAME_RE.match(name):
            raise NotFound(name)
        meta = self._load(name)
        tname = meta["tmux"] if meta else SESSION_PREFIX + name
        alive = tname in self.tmux.sessions()
        if meta is None and not alive:
            raise NotFound(name)
        return meta, tname, alive

    def stop(self, name: str) -> None:
        """Kill the session (the transcript stays, so `continue` picks it up) and forget it here."""
        _, tname, alive = self._require(name)
        if alive:
            log.info("stop %s", name)
            self.tmux.kill(tname)
            self.reap(self.tmux.sessions())
        shutil.rmtree(self.sessions_dir / name, ignore_errors=True)

    def answer(self, name: str, body: Mapping[str, Any]) -> None:
        _, tname, alive = self._require(name)
        if not alive:
            raise BadRequest("session is not running")
        if body.get("escape"):
            self.tmux.send_keys(tname, ["Escape"])
            return
        dialog = parse_pane(self.tmux.capture(tname)).dialog
        if dialog is None:
            raise BadRequest("no menu is waiting for input")
        target = body.get("option")
        if not isinstance(target, int) or not 0 <= target < len(dialog.options):
            raise BadRequest("option out of range")
        log.info("answer %s: '%s'", name, dialog.options[target])
        self.tmux.send_keys(tname, keys_for_option(dialog.selected, target))


# --------------------------------------------------------------------------- HTTP


def make_handler(app: RCLauncher) -> type[BaseHTTPRequestHandler]:
    cfg = app.cfg
    local_hosts = {f"127.0.0.1:{cfg.port}", f"localhost:{cfg.port}", "127.0.0.1", "localhost"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "claude-rc-launcher"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.headers.get("Tailscale-User-Login", "-") if self.headers else "-", fmt % args)

        # ---- responses

        def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(status, {"error": message})

        # ---- guards

        def _host_ok(self) -> bool:
            if not cfg.public_hosts:
                return True
            hosts = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", "")} - {""}
            return bool(hosts) and all(h.lower() in cfg.public_hosts | local_hosts for h in hosts)

        def _identity_ok(self) -> bool:
            return not cfg.allowed_logins or self.headers.get("Tailscale-User-Login", "") in cfg.allowed_logins

        def _csrf_problem(self) -> str | None:
            ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return "Content-Type must be application/json"
            if self.headers.get("X-RC-Launcher") != "1":
                return "missing X-RC-Launcher header"
            origin = self.headers.get("Origin")
            if origin and origin != "null":
                allowed = {self.headers.get("Host", ""), self.headers.get("X-Forwarded-Host", "")} | cfg.public_hosts
                if urlsplit(origin).netloc.lower() not in {h.lower() for h in allowed if h}:
                    return "cross-origin request refused"
            return None

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                raise BadRequest("request body too large")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError as exc:
                raise BadRequest("body is not valid JSON") from exc
            if not isinstance(data, dict):
                raise BadRequest("body must be a JSON object")
            return data

        # ---- routing

        def do_HEAD(self) -> None:
            self.do_GET()

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/healthz":
                return self._json(200, {"ok": True})
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "unknown Host")
            if not self._identity_ok():
                return self._error(HTTPStatus.FORBIDDEN, "this tailnet identity is not allowed")
            if path in STATIC_FILES:
                fname, ctype = STATIC_FILES[path]
                try:
                    body = (STATIC_DIR / fname).read_bytes()
                except OSError:
                    return self._error(HTTPStatus.NOT_FOUND, "not found")
                return self._send(200, body, ctype, "no-cache")
            if path == "/api/projects":
                return self._json(200, {"projects": app.projects(), "modes": MODES, "default_mode": cfg.default_mode})
            if path == "/api/sessions":
                return self._json(
                    200,
                    {"host": cfg.host_label, "claude_version": app.claude_version, "now": time.time(),
                     "sessions": app.list_sessions()},
                )  # fmt: skip
            return self._error(HTTPStatus.NOT_FOUND, "not found")

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if not self._host_ok():
                return self._error(HTTPStatus.FORBIDDEN, "unknown Host")
            if not self._identity_ok():
                return self._error(HTTPStatus.FORBIDDEN, "this tailnet identity is not allowed")
            problem = self._csrf_problem()
            if problem:
                log.warning("refused POST %s: %s (origin=%s)", path, problem, self.headers.get("Origin"))
                return self._error(HTTPStatus.FORBIDDEN, problem)
            try:
                body = self._body()
                if path == "/api/start":
                    return self._json(200, app.start(body))
                m = re.fullmatch(r"/api/sessions/([^/]+)/(stop|answer)", path)
                if m:
                    name = unquote(m.group(1))
                    if m.group(2) == "stop":
                        app.stop(name)
                    else:
                        app.answer(name, body)
                    return self._json(200, {"ok": True})
                return self._error(HTTPStatus.NOT_FOUND, "not found")
            except BadRequest as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except NotFound as exc:
                return self._error(HTTPStatus.NOT_FOUND, f"no session {exc}")
            except LaunchError as exc:
                return self._error(HTTPStatus.BAD_GATEWAY, str(exc))

    return Handler


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("RC_LAUNCHER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    cfg = Config.from_env(os.environ)
    app = RCLauncher(cfg)
    try:
        app.claude_version = run_cmd([cfg.claude_bin, "--version"]).stdout.strip().split(" ")[0]
    except (OSError, subprocess.SubprocessError):
        log.warning("could not run %s --version", cfg.claude_bin)
    for tool in (cfg.tmux_bin, cfg.claude_bin, cfg.ghostty_app):
        if not Path(tool).exists():
            log.warning("%s not found; launches will fail", tool)
    httpd = ThreadingHTTPServer((cfg.bind, cfg.port), make_handler(app))
    log.info(
        "listening on http://%s:%s roots=%s socket=%s logins=%s hosts=%s claude=%s",
        cfg.bind, cfg.port, ":".join(map(str, cfg.roots)), cfg.socket,
        ",".join(sorted(cfg.allowed_logins)) or "(any)", ",".join(sorted(cfg.public_hosts)) or "(any)",
        app.claude_version or "?",
    )  # fmt: skip
    httpd.serve_forever()


if __name__ == "__main__":
    main()
