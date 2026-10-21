# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added
- `rc_launcher.py`: stdlib-only daemon that starts `claude --remote-control` sessions inside Ghostty → tmux (dedicated `claude-rc` socket), so sessions inherit Ghostty's privacy permissions and the unlocked login keychain.
- Session watcher: accepts the folder-trust prompt (and the bypass-permissions warning when that mode was chosen), records the Remote Control link, and marks launches that never reach tmux as failed.
- Menu passthrough: any select menu Claude Code shows in the terminal is surfaced on the page and answered from it.
- Reaping of Ghostty instances whose session has ended, matched by the launcher's own tmux signature only.
- PWA page: project picker with most-recent-first ordering and preselection, first message, permission mode, `--continue`, live session cards with keyed DOM updates, English and Chinese (`?lang=`), light and dark themes.
- Guards: Tailscale identity allowlist, Host allowlist, JSON + custom-header + Origin checks on every POST, project paths confined to configured roots.
- `scripts/install.sh`: uv environment, Full Disk Access request and verification for Ghostty, LaunchAgent install/update/uninstall, health check.
- `scripts/demo.py`: the page against made-up sessions, for UI work and screenshots.
