"""Tests for claude-launchpad. External commands (open, tmux) go through a fake runner."""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import launchpad as lp

TRUST_PANE = textwrap.dedent(
    """\
    ────────────────────────────────────────────────────────────────
     Accessing workspace:

     /private/tmp/lp-probe

     Quick safety check: Is this a project you created or one you trust?

     Claude Code'll be able to read, edit, and execute files here.

     Security guide

     ❯ No, exit
       Yes, I trust this folder

     Enter to confirm · Esc to cancel
    """
)

RUNNING_PANE = textwrap.dedent(
    """\
     ▐▛███▛█   Claude Code v2.1.306
    ▝▜██████▀  Fable 5.1 with high effort · Claude Max
     ▝▝   ▝▝   /private/tmp/lp-probe

    ❯ say hi in 3 words

      /remote-control is active · Continue here, on your phone, or at https://claude.ai/code/session_01RMsXTCpV6y6oBpE3UFnCMQ

    ⏺ hi there friend

    ✻ Sautéed for 4s · done 11:58 AM
    ────────────────────────────────────────────────────────────────
    ❯
    ────────────────────────────────────────────────────────────────
        [Fable 5.1] 6% context                                  57475 tokens
      ⏵⏵ auto mode on (shift+tab to cycle)
    """
)

NUMBERED_DIALOG_PANE = RUNNING_PANE + textwrap.dedent(
    """\
    ────────────────────────────────────────────────────────────────
      Teach auto mode about your environment?

      Auto mode works better when it knows your environment. Takes about a minute.

      ❯ 1. Yes
        2. Not now
        3. Don't show again

      Enter to confirm · Esc to cancel
    """
)


# --------------------------------------------------------------------------- helpers


class FakeRunner:
    """Records argv lists; answers tmux commands from a tiny in-memory model."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.live: dict[str, str] = {}  # tmux session name -> pane text
        self.open_rc = 0

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0].endswith("open"):
            return subprocess.CompletedProcess(argv, self.open_rc, "", "boom" if self.open_rc else "")
        assert argv[1:3] == ["-L", "launchpad"], argv
        cmd = argv[3]
        if cmd == "list-sessions":
            if not self.live:
                return subprocess.CompletedProcess(argv, 1, "", "no server running")
            out = "".join(f"{n}\t1792600000\t0\n" for n in self.live)
            return subprocess.CompletedProcess(argv, 0, out, "")
        target = argv[argv.index("-t") + 1].strip("=:")
        if cmd == "capture-pane":
            return subprocess.CompletedProcess(argv, 0, self.live.get(target, ""), "")
        if cmd == "send-keys":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if cmd == "kill-session":
            self.live.pop(target, None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(f"unexpected command {argv}")

    def sent_keys(self) -> list[list[str]]:
        return [c[c.index("-t") + 2 :] for c in self.calls if len(c) > 3 and c[3] == "send-keys"]


@pytest.fixture()
def roots(tmp_path: Path) -> Path:
    codes = tmp_path / "Codes"
    (codes / "alpha").mkdir(parents=True)
    (codes / "beta").mkdir()
    (codes / ".hidden").mkdir()
    (codes / "notes.txt").write_text("x")
    elsewhere = tmp_path / "ssd" / "gamma"
    elsewhere.mkdir(parents=True)
    (codes / "gamma").symlink_to(elsewhere)
    (tmp_path / "outside").mkdir()
    return codes


@pytest.fixture()
def cfg(tmp_path: Path, roots: Path) -> lp.Config:
    return lp.Config(
        roots=(roots,),
        state_dir=tmp_path / "state",
        tmux_bin="/opt/homebrew/bin/tmux",
        claude_bin="/Users/x/.local/bin/claude",
        allowed_logins=frozenset({"me@example.com"}),
        public_hosts=frozenset({"launchpad.example.ts.net"}),
        launch_timeout=45.0,
    )


@pytest.fixture()
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture()
def app(cfg: lp.Config, runner: FakeRunner) -> lp.Launchpad:
    return lp.Launchpad(cfg, run=runner, watch=False)


# --------------------------------------------------------------------------- names & paths


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my project", "my-project"),
        ("a/b;rm -rf ~", "a-b-rm-rf"),
        ("--weird--", "weird"),
        ("中文名", "session"),
        ("x" * 80, "x" * 40),
    ],
)
def test_sanitize_name(raw: str, expected: str) -> None:
    assert lp.sanitize_name(raw) == expected


@pytest.mark.unit
def test_resolve_project_accepts_root_children_and_symlink_targets(cfg: lp.Config, roots: Path) -> None:
    assert lp.resolve_project(cfg, str(roots / "alpha")) == (roots / "alpha").resolve()
    gamma = lp.resolve_project(cfg, str(roots / "gamma"))
    assert gamma == (roots / "gamma").resolve()
    sub = gamma / "sub"
    sub.mkdir()
    assert lp.resolve_project(cfg, str(sub)) == sub


@pytest.mark.unit
@pytest.mark.parametrize("rel", ["../outside", "notes.txt", "missing", "alpha/../../outside"])
def test_resolve_project_rejects(cfg: lp.Config, roots: Path, rel: str) -> None:
    with pytest.raises(lp.BadRequest):
        lp.resolve_project(cfg, str(roots / rel))


@pytest.mark.unit
def test_resolve_project_rejects_relative(cfg: lp.Config) -> None:
    with pytest.raises(lp.BadRequest):
        lp.resolve_project(cfg, "alpha")


@pytest.mark.unit
def test_projects_lists_dirs_recent_first(app: lp.Launchpad, roots: Path) -> None:
    app.history.touch(str((roots / "beta").resolve()))
    names = [p["name"] for p in app.projects()]
    assert names[0] == "beta"
    assert set(names) == {"alpha", "beta", "gamma"}


# --------------------------------------------------------------------------- launch commands


@pytest.mark.unit
def test_ghostty_argv(cfg: lp.Config, roots: Path) -> None:
    argv = lp.ghostty_argv(cfg, "lp-alpha", roots / "alpha", Path("/s/run.sh"), "alpha")
    assert argv[:4] == ["/usr/bin/open", "-na", "/Applications/Ghostty.app", "--args"]
    e = argv.index("-e")
    assert argv[e + 1 :] == [
        "/opt/homebrew/bin/tmux",
        "-L",
        "launchpad",
        "new-session",
        "-A",
        "-s",
        "lp-alpha",
        "-c",
        str(roots / "alpha"),
        "/s/run.sh",
    ]
    assert "--quit-after-last-window-closed=true" in argv[:e]


@pytest.mark.integration
@pytest.mark.parametrize("cont", [False, True])
def test_run_script_passes_args_verbatim(tmp_path: Path, cont: bool) -> None:
    """Execute the generated script with stub claude/caffeinate and check the exact argv."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    argv_file = tmp_path / "argv.json"
    (bindir / "caffeinate").write_text('#!/bin/bash\nshift\n"$@"\n')
    (bindir / "claude").write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd()}}, open({str(argv_file)!r}, 'w'))\n"
    )
    for f in bindir.iterdir():
        f.chmod(0o755)
    prompt = 'it\'s "quoted" $HOME `id`\nsecond line'
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(prompt)
    work = tmp_path / "work dir"
    work.mkdir()
    cfg = lp.Config(claude_bin=str(bindir / "claude"), session_path=f"{bindir}:/usr/bin:/bin")
    script = lp.render_run_script(
        cfg, workdir=work, name="alpha", mode="auto", cont=cont, prompt_file=prompt_file, keep_shell=False
    )
    out = subprocess.run(["/bin/bash", "-c", script], check=False, capture_output=True, text=True, timeout=20)
    assert "[launchpad] claude exited (0)" in out.stdout, out.stderr
    got = json.loads(argv_file.read_text())
    expected = ["--remote-control=alpha", "--permission-mode", "auto"]
    if cont:
        expected.append("--continue")
    assert got["argv"] == [*expected, prompt]
    assert Path(got["cwd"]).resolve() == work.resolve()


# --------------------------------------------------------------------------- pane parsing


@pytest.mark.unit
def test_parse_trust_dialog() -> None:
    st = lp.parse_pane(TRUST_PANE)
    assert st.state == "needs-input"
    assert st.dialog is not None
    assert st.dialog.title == "Accessing workspace:"
    assert st.dialog.options == ["No, exit", "Yes, I trust this folder"]
    assert st.dialog.selected == 0


@pytest.mark.unit
def test_parse_numbered_dialog_over_running_session() -> None:
    st = lp.parse_pane(NUMBERED_DIALOG_PANE)
    assert st.state == "needs-input"
    assert st.dialog is not None
    assert st.dialog.title == "Teach auto mode about your environment?"
    assert st.dialog.options == ["1. Yes", "2. Not now", "3. Don't show again"]
    assert st.rc_url == "https://claude.ai/code/session_01RMsXTCpV6y6oBpE3UFnCMQ"


@pytest.mark.unit
def test_parse_running_idle_and_busy() -> None:
    st = lp.parse_pane(RUNNING_PANE)
    assert (st.state, st.busy, st.dialog) == ("running", False, None)
    busy = lp.parse_pane(RUNNING_PANE.replace("✻ Sautéed for 4s · done 11:58 AM", "✻ Thinking… (esc to interrupt)"))
    assert busy.busy is True


@pytest.mark.unit
def test_parse_exited_and_starting_and_login() -> None:
    st = lp.parse_pane(RUNNING_PANE + "\n[launchpad] claude exited (1)\nlishuyu@mini ~ %\n")
    assert (st.state, st.exit_code) == ("exited", 1)
    assert lp.parse_pane("").state == "starting"
    assert lp.parse_pane("Select login method:\n ❯ 1. Claude account\n").state == "needs-login"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("selected", "target", "keys"), [(0, 1, ["Down", "Enter"]), (2, 0, ["Up", "Up", "Enter"]), (1, 1, ["Enter"])]
)
def test_keys_for_option(selected: int, target: int, keys: list[str]) -> None:
    assert lp.keys_for_option(selected, target) == keys


# --------------------------------------------------------------------------- launcher flow


@pytest.mark.unit
def test_start_writes_files_and_opens_ghostty(
    app: lp.Launchpad, runner: FakeRunner, roots: Path, cfg: lp.Config
) -> None:
    meta = app.start({"dir": str(roots / "alpha"), "prompt": "hello", "mode": "plan"})
    assert meta["name"] == "alpha"
    sdir = cfg.state_dir / "sessions" / "alpha"
    assert (sdir / "prompt.md").read_text() == "hello"
    assert "--permission-mode plan" in (sdir / "run.sh").read_text()
    assert os.access(sdir / "run.sh", os.X_OK)
    opens = [c for c in runner.calls if c[0].endswith("open")]
    assert len(opens) == 1 and opens[0][-1] == str(sdir / "run.sh")
    assert app.list_sessions()[0]["state"] == "launching"


@pytest.mark.unit
def test_start_uniquifies_names(app: lp.Launchpad, runner: FakeRunner, roots: Path) -> None:
    runner.live["lp-alpha"] = RUNNING_PANE
    meta = app.start({"dir": str(roots / "alpha")})
    assert meta["name"] == "alpha-2"


@pytest.mark.unit
@pytest.mark.parametrize("body", [{"dir": "/etc"}, {"dir": "x"}, {}, {"dir": "/tmp", "mode": "yolo"}])
def test_start_rejects_bad_input(app: lp.Launchpad, body: dict) -> None:
    with pytest.raises(lp.BadRequest):
        app.start(body)


@pytest.mark.unit
def test_start_reports_open_failure(app: lp.Launchpad, runner: FakeRunner, roots: Path) -> None:
    runner.open_rc = 1
    with pytest.raises(lp.LaunchError):
        app.start({"dir": str(roots / "alpha")})


@pytest.mark.unit
def test_watch_step_accepts_trust_once_then_saves_rc_url(app: lp.Launchpad, runner: FakeRunner, roots: Path) -> None:
    app.start({"dir": str(roots / "alpha")})
    runner.live["lp-alpha"] = TRUST_PANE
    flags: dict[str, bool] = {}
    assert app.watch_step("alpha", flags) is False
    assert app.watch_step("alpha", flags) is False  # still on screen: answered only once
    assert runner.sent_keys() == [["Down", "Enter"]]
    runner.live["lp-alpha"] = RUNNING_PANE
    assert app.watch_step("alpha", flags) is True
    sess = app.list_sessions()[0]
    assert sess["state"] == "running"
    assert sess["rc_url"].endswith("session_01RMsXTCpV6y6oBpE3UFnCMQ")


@pytest.mark.unit
def test_watch_step_marks_failed_launch(app: lp.Launchpad, roots: Path) -> None:
    app.start({"dir": str(roots / "alpha")})
    app.clock = lambda: lp.time.time() + 999
    assert app.watch_step("alpha", {}) is True
    assert app.list_sessions()[0]["state"] == "failed"


@pytest.mark.unit
def test_answer_and_stop(app: lp.Launchpad, runner: FakeRunner, roots: Path, cfg: lp.Config) -> None:
    app.start({"dir": str(roots / "alpha")})
    runner.live["lp-alpha"] = NUMBERED_DIALOG_PANE
    sess = app.list_sessions()[0]
    assert sess["state"] == "needs-input" and sess["dialog"]["options"][2] == "3. Don't show again"
    app.answer("alpha", {"option": 1})
    app.answer("alpha", {"escape": True})
    assert runner.sent_keys() == [["Down", "Enter"], ["Escape"]]
    with pytest.raises(lp.BadRequest):
        app.answer("alpha", {"option": 7})
    app.stop("alpha")
    assert "lp-alpha" not in runner.live
    assert not (cfg.state_dir / "sessions" / "alpha").exists()
    with pytest.raises(lp.NotFound):
        app.stop("alpha")


@pytest.mark.unit
def test_ended_and_orphan_sessions(app: lp.Launchpad, runner: FakeRunner, roots: Path) -> None:
    app.start({"dir": str(roots / "alpha")})
    runner.live["lp-alpha"] = RUNNING_PANE
    app.list_sessions()  # marks it seen
    del runner.live["lp-alpha"]
    runner.live["lp-orphan"] = RUNNING_PANE
    states = {s["name"]: s["state"] for s in app.list_sessions()}
    assert states == {"alpha": "ended", "orphan": "running"}


# --------------------------------------------------------------------------- HTTP layer


@pytest.fixture()
def server(app: lp.Launchpad) -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), lp.make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(
    url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None
) -> tuple[int, dict | str]:
    h = {"Tailscale-User-Login": "me@example.com", "Host": "launchpad.example.ts.net"}
    if method == "POST":
        h |= {"Content-Type": "application/json", "X-Launchpad": "1", "Origin": "https://launchpad.example.ts.net"}
    h |= headers or {}
    h = {k: v for k, v in h.items() if v is not None}
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


@pytest.mark.integration
def test_http_identity_and_host_checks(server: str) -> None:
    assert call(server + "/healthz", headers={"Tailscale-User-Login": None})[0] == 200
    assert call(server + "/api/sessions")[0] == 200
    assert call(server + "/api/sessions", headers={"Tailscale-User-Login": None})[0] == 403
    assert call(server + "/api/sessions", headers={"Tailscale-User-Login": "eve@example.com"})[0] == 403
    assert call(server + "/api/sessions", headers={"Host": "evil.example:8795"})[0] == 403
    status, page = call(server + "/")
    assert status == 200 and "<html" in page


@pytest.mark.integration
@pytest.mark.parametrize(
    "bad",
    [{"X-Launchpad": None}, {"Content-Type": "text/plain"}, {"Origin": "https://evil.example"}],
)
def test_http_post_csrf_guards(server: str, roots: Path, runner: FakeRunner, bad: dict) -> None:
    status, _ = call(server + "/api/start", "POST", {"dir": str(roots / "alpha")}, bad)
    assert status == 403
    assert not [c for c in runner.calls if c[0].endswith("open")]


@pytest.mark.integration
def test_http_start_list_stop(server: str, roots: Path, runner: FakeRunner) -> None:
    status, body = call(server + "/api/projects")
    assert status == 200 and {p["name"] for p in body["projects"]} == {"alpha", "beta", "gamma"}
    status, body = call(server + "/api/start", "POST", {"dir": str(roots / "beta")})
    assert status == 200 and body["name"] == "beta"
    assert call(server + "/api/start", "POST", {"dir": "/etc"})[0] == 400
    runner.live["lp-beta"] = RUNNING_PANE
    status, body = call(server + "/api/sessions")
    assert body["sessions"][0]["state"] == "running"
    assert call(server + "/api/sessions/beta/stop", "POST")[0] == 200
    assert call(server + "/api/sessions/nope/stop", "POST")[0] == 404
    assert call(server + "/api/sessions/..%2Fx/stop", "POST")[0] == 404
