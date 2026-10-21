#!/bin/bash
# install.sh — install (or update) claude-launchpad as a LaunchAgent on the Mac that hosts the sessions.
# Run it on that Mac, from the repo checkout. Re-running it redeploys.
#
# usage: scripts/install.sh [--skip-fda] [--uninstall]
#   --skip-fda    don't (re)check Ghostty's Full Disk Access
#   --uninstall   stop and remove the LaunchAgent (sessions that are running stay up)
#
# Optional environment (written into the LaunchAgent):
#   LAUNCHPAD_ALLOWED_LOGINS  comma-separated Tailscale logins allowed in (recommended)
#   LAUNCHPAD_PUBLIC_HOSTS    comma-separated host names the page is served under, e.g. launchpad.<tailnet>.ts.net
#   LAUNCHPAD_ROOTS           colon-separated project roots (default ~/Codes)
#   LAUNCHPAD_HOST_LABEL      machine name shown in the page header (default: short hostname)
#   LAUNCHPAD_DEFAULT_MODE    default permission mode (default auto)
#   LAUNCHPAD_PORT            local port (default 8795)
#   LAUNCHPAD_LABEL           LaunchAgent label (default app.claude-launchpad)
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd -P)
LABEL=${LAUNCHPAD_LABEL:-app.claude-launchpad}
PORT=${LAUNCHPAD_PORT:-8795}
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs/claude-launchpad"
GHOSTTY=/Applications/Ghostty.app
SESSION_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH="$SESSION_PATH"
SKIP_FDA=0
UNINSTALL=0
for arg in "$@"; do
  case $arg in
    --skip-fda) SKIP_FDA=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) sed -n '2,17p' "$0" >&2; exit 2 ;;
  esac
done

log() { printf '[launchpad] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

if ((UNINSTALL)); then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  [[ -f $PLIST ]] && cp "$PLIST" "$PLIST.bak.$(date +%Y%m%d%H%M%S)" && rm "$PLIST"
  log "removed $LABEL (backup of the plist kept next to it); running sessions are untouched"
  exit 0
fi

# --- prerequisites -------------------------------------------------------------------------
[[ $(uname -s) == Darwin ]] || die "macOS only"
[[ -d $GHOSTTY ]] || die "Ghostty not found at $GHOSTTY — brew install --cask ghostty, then open it once"
command -v tmux >/dev/null || die "tmux not found — brew install tmux"
command -v claude >/dev/null || die "claude not found — https://docs.claude.com/en/docs/claude-code"
command -v uv >/dev/null || die "uv not found — brew install uv"

# --- Python (stdlib only; a uv-managed interpreter keeps it independent of Homebrew's) --------
uv venv --quiet --allow-existing --python 3.13 "$REPO/.venv"
PY="$REPO/.venv/bin/python"
log "python: $("$PY" --version)"

# --- Full Disk Access for Ghostty ------------------------------------------------------------
# Sessions run inside Ghostty and inherit its privacy grants. macOS has no prompt for Full Disk
# Access, but an app that touches FDA-protected data gets listed (switched off) in System Settings.
# So: let Ghostty read the system TCC database (readable only with FDA), then open the right pane
# and check again until it is on.
probe_fda() {
  local out
  out=$(mktemp -t launchpad-fda)
  open -na "$GHOSTTY" --args --title="launchpad · permission check" --quit-after-last-window-closed=true \
    --window-save-state=never -e /bin/bash -c \
    "if head -c 16 '/Library/Application Support/com.apple.TCC/TCC.db' >/dev/null 2>&1; then echo granted; else echo denied; fi > '$out'"
  for _ in $(seq 1 30); do
    [[ -s $out ]] && break
    sleep 0.5
  done
  cat "$out" 2>/dev/null || true
  rm -f "$out"
}
if ((SKIP_FDA == 0)); then
  for attempt in 1 2 3 4; do
    result=$(probe_fda)
    case $result in
      granted) log "Ghostty has Full Disk Access"; break ;;
      denied)
        ((attempt == 4)) && die "Ghostty still lacks Full Disk Access"
        open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_AllFiles"
        log "Ghostty needs Full Disk Access. System Settings is open on this Mac at Privacy & Security → Full Disk Access;"
        log "Ghostty is listed there now — switch it on (use + → /Applications/Ghostty.app if it is missing)."
        [[ -t 0 ]] || die "re-run this script after enabling it (or pass --skip-fda)"
        read -r -p "[launchpad] press Enter once it is on… " _
        ;;
      *) die "the Ghostty permission check did not report back (is someone logged in to this Mac's desktop?)" ;;
    esac
  done
fi

# --- LaunchAgent -------------------------------------------------------------------------------
mkdir -p "$LOGDIR" "$(dirname "$PLIST")"
[[ -f $PLIST ]] && cp "$PLIST" "$PLIST.bak.$(date +%Y%m%d%H%M%S)"
LABEL=$LABEL PLIST=$PLIST LOGDIR=$LOGDIR REPO=$REPO PY=$PY PORT=$PORT SESSION_PATH=$SESSION_PATH "$PY" - <<'EOF'
import os, plistlib
env = {
    "HOME": os.environ["HOME"],
    "PATH": os.environ["SESSION_PATH"],
    "LANG": "en_US.UTF-8",
    "LAUNCHPAD_PORT": os.environ["PORT"],
}
for key in ("LAUNCHPAD_ALLOWED_LOGINS", "LAUNCHPAD_PUBLIC_HOSTS", "LAUNCHPAD_ROOTS", "LAUNCHPAD_HOST_LABEL",
            "LAUNCHPAD_DEFAULT_MODE"):
    if os.environ.get(key):
        env[key] = os.environ[key]
plist = {
    "Label": os.environ["LABEL"],
    "ProgramArguments": [os.environ["PY"], os.path.join(os.environ["REPO"], "launchpad.py")],
    "WorkingDirectory": os.environ["REPO"],
    "EnvironmentVariables": env,
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Interactive",
    "StandardOutPath": os.path.join(os.environ["LOGDIR"], "stdout.log"),
    "StandardErrorPath": os.path.join(os.environ["LOGDIR"], "stderr.log"),
}
with open(os.environ["PLIST"], "wb") as f:
    plistlib.dump(plist, f)
EOF
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
log "loaded $LABEL ($PLIST)"

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    log "up on http://127.0.0.1:$PORT — logs in $LOGDIR"
    log "share it on your tailnet, e.g.: tailscale serve --bg --https=443 --service=svc:launchpad http://127.0.0.1:$PORT"
    exit 0
  fi
  sleep 0.5
done
tail -20 "$LOGDIR/stderr.log" >&2 || true
die "the daemon did not come up on port $PORT"
