# Setup and Operations

openCodexMicro automatically selects one of two state sources:

```text
Codex renderer Micro store ──> Bridge cache ──> D200
              unavailable ──> local app-server/rollout fallback
```

| How Codex was launched | State and task-key navigation |
| --- | --- |
| `Codex Bridge.app` | Renderer Micro store plus official Micro event bus |
| Normal Codex app | Local-only app-server/rollout state plus `codex://` links |

No daemon setting selects the mode. Bridge state includes local and Codex SSH
tasks, Most Recent order, status, selection, and host routing. When it is
unavailable, openCodexMicro starts `NativeCodex(enable_remote=False)` and does
not connect to SSH hosts or read their SQLite databases.

## Dependencies

- macOS;
- Codex Desktop;
- Ulanzi D200 over USB;
- Node.js 20 or newer;
- Python 3.11 or newer with `venv`;
- Accessibility permission for action shortcuts and the normal-mode SSH Dock
  Recent fallback.

The installer creates an isolated runtime venv and installs:

| Dependency | Purpose |
| --- | --- |
| `hidapi` | Read D200 keys and send HID profiles |
| Pillow | Render and quantize key images |

The runtime venv is stored at:

```text
~/Library/Application Support/openCodexMicro/venv/
```

## Install

```bash
npm install
npm run setup
```

The installer validates Python before changing active services, builds the
bridge, creates the private runtime directory, and starts the D200 and bridge
sidecar LaunchAgents. It also installs and ad-hoc signs:

```text
~/Applications/Codex Bridge.app
```

The installer adds `realtimeVoice.toggleMicrophoneMute → Command+Alt+M` only
when that Codex command has no existing shortcut override. It is the Mic
fallback; Bridge mode uses the official Micro press/release path.

To install files without starting either LaunchAgent:

```bash
npm run setup -- --no-start
```

### Migration from CodexKeyboard

The installer stops and removes old native and bridge LaunchAgents. An old
`icon-theme.json` is migrated to openCodexMicro and retained as
`icon-theme.legacy-backup.json` before the obsolete runtime is removed.

## Starting Codex

For direct official-Micro navigation:

1. Quit the currently running Codex app.
2. Double-click `~/Applications/Codex Bridge.app`.

The wrapper launches `/Applications/ChatGPT.app/Contents/MacOS/ChatGPT`
directly with:

```text
--remote-debugging-address=127.0.0.1
--remote-debugging-port=9222
--remote-allow-origins=http://127.0.0.1:9222
```

It waits for `/json/version`. If the endpoint does not become available, the
wrapper displays an error explaining that task keys will use normal fallback.
Finder does not provide persistent per-app launch arguments; this wrapper is
the supported double-click launcher.

Opening the original Codex icon remains valid. In that case only local tasks
are displayed and task keys use deep links. Launch through Bridge to display
and navigate remote tasks.

CDP binds to `127.0.0.1:9222` and the sidecar to `127.0.0.1:17373`; neither is
reachable from the LAN.

## macOS permissions

Open:

```text
System Settings → Privacy & Security → Accessibility
```

Allow the installed Python process to control `System Events`, and approve
automation prompts if macOS asks. Bridge task switching and Focus work without
this permission; AppleScript action keys and normal-mode SSH fallback do not.

## Installed files and services

Runtime files, venv, theme, caches, and logs:

```text
~/Library/Application Support/openCodexMicro/
```

LaunchAgents:

```text
~/Library/LaunchAgents/io.opencodexmicro.d200.plist
~/Library/LaunchAgents/io.opencodexmicro.bridge.plist
```

Inspect or restart them:

```bash
launchctl print "gui/$(id -u)/io.opencodexmicro.d200"
launchctl print "gui/$(id -u)/io.opencodexmicro.bridge"
launchctl kickstart -k "gui/$(id -u)/io.opencodexmicro.d200"
launchctl kickstart -k "gui/$(id -u)/io.opencodexmicro.bridge"
```

Logs:

```bash
tail -f "$HOME/Library/Application Support/openCodexMicro/d200.log"
tail -f "$HOME/Library/Application Support/openCodexMicro/d200-error.log"
tail -f "$HOME/Library/Application Support/openCodexMicro/bridge.log"
tail -f "$HOME/Library/Application Support/openCodexMicro/bridge-error.log"
```

## Diagnostics

From the repository:

```bash
# Validate rendering, profile ZIP generation, and HID framing.
python3 standalone/d200.py --self-test

# Read one Codex state snapshot without opening the D200.
python3 standalone/d200.py --state

# Explicit legacy local/SSH diagnostic (may open SSH monitors).
python3 standalone/d200.py --native-state

# Identify the HID interface without writing to it.
python3 standalone/d200.py --diagnose

# Bridge sidecar and current Codex connection.
curl http://127.0.0.1:17373/health
curl http://127.0.0.1:17373/state
```

If the current Python lacks runtime dependencies:

```bash
"$HOME/Library/Application Support/openCodexMicro/venv/bin/python" \
  standalone/d200.py --self-test
```

| Symptom | Check |
| --- | --- |
| D200 is missing or reconnecting | USB cable, port, `d200-error.log`, and `--diagnose` |
| D200 starts or reconnects but some keys stay stale | Confirm the log shows `uploading full profile`; version 0.3.0 treats every new HID session as an unknown framebuffer |
| Codex Micro says `Not detected` | Launch Codex through `Codex Bridge.app`; inspect the process for `--remote-debugging-port=9222` |
| Bridge task key does not switch | Check `bridge-error.log` and the sidecar health endpoint |
| Normal-mode local task does not switch | Check whether Codex handles `codex://threads/<id>` |
| Remote task is missing | Launch through `Codex Bridge.app`; normal mode intentionally shows local tasks only |
| Steer does nothing | Launch through `Codex Bridge.app`, check bridge health, and confirm a running task exposes the composer Steer action |
| Mic does nothing | In Bridge mode check bridge health; otherwise verify `realtimeVoice.toggleMicrophoneMute` in `~/.codex/keybindings.json` |
| Pin or New does nothing | Verify Accessibility and the shortcut directly in Codex |
| Theme changes do not appear | Reset the display digest as described in [Configuration](configuration.md#theme) |
| Usage is temporarily empty | Wait for initial app-server data and check the Codex account connection |

## Update

```bash
npm install
npm run check
npm run setup
```

## Uninstall

```bash
npm run uninstall
```

This stops both openCodexMicro LaunchAgents, removes `Codex Bridge.app`, and
deletes current and recognized legacy runtime directories. Back up a custom
theme first.

## Development

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r standalone/requirements.txt
npm run check
```

Available commands:

```bash
npm test
npm run build:bridge
npm run check
```

Main entry points:

- `standalone/d200.py`: HID, profiles, input path, and display queue;
- `standalone/native_codex.py`: Bridge-first adapter, local fallback, explicit legacy SSH diagnostics, navigation, and shortcuts;
- `src/bridge/`: official Codex Micro renderer bridge;
- `scripts/build-bridge.mjs`: bundle the loopback sidecar;
- `scripts/install.mjs`: installation, wrapper packaging, and migration;
- `scripts/uninstall.mjs`: service, wrapper, and runtime removal.
