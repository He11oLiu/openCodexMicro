# Setup and Operations

openCodexMicro has one native integration path:

```text
Codex app-server + rollout events + codex:// deep links
                         ↓
                  openCodexMicro
                         ↓
                   Ulanzi D200
```

It does not open a Chromium debugging port or run a local HTTP service.

## Dependencies

- macOS;
- Codex Desktop;
- Ulanzi D200 over USB;
- Node.js 20 or newer for the installer;
- Python 3.11 or newer with `venv`.

The installer creates an isolated runtime environment and installs:

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
npm run setup
```

The installer validates Python before changing the active service, creates the
runtime directory with private permissions, installs dependencies, and starts
the LaunchAgent.

To install the LaunchAgent without starting the daemon:

```bash
npm run setup -- --no-start
```

### Migration from CodexKeyboard

The installer stops and removes the old native and bridge LaunchAgents. If an
old `icon-theme.json` exists, it is migrated to openCodexMicro and also kept as
`icon-theme.legacy-backup.json`. The obsolete CodexKeyboard runtime directory
is then removed.

## macOS permissions

Actions that simulate keyboard input require Accessibility permission. Open:

```text
System Settings → Privacy & Security → Accessibility
```

Allow the Python process installed under the openCodexMicro application
support directory. Also approve control of `System Events` if macOS asks.

Without this permission, task switching and Focus still work, but
AppleScript-based action keys do not.

## Installed files and service

Runtime files, the Python venv, theme, caches, and logs:

```text
~/Library/Application Support/openCodexMicro/
```

LaunchAgent:

```text
~/Library/LaunchAgents/io.opencodexmicro.d200.plist
```

Inspect the service:

```bash
launchctl print "gui/$(id -u)/io.opencodexmicro.d200"
```

Restart the driver:

```bash
launchctl kickstart -k "gui/$(id -u)/io.opencodexmicro.d200"
```

Logs:

```bash
tail -f "$HOME/Library/Application Support/openCodexMicro/d200.log"
tail -f "$HOME/Library/Application Support/openCodexMicro/d200-error.log"
```

## Diagnostics

From the repository:

```bash
# Validate rendering, profile ZIP generation, and HID framing without a device.
python3 standalone/d200.py --self-test

# Read one Codex state snapshot without opening the D200.
python3 standalone/d200.py --state

# Open and identify the HID interface without writing to it.
python3 standalone/d200.py --diagnose
```

If the current Python does not have the runtime dependencies, use the installed
venv:

```bash
"$HOME/Library/Application Support/openCodexMicro/venv/bin/python" \
  standalone/d200.py --self-test
```

| Symptom | Check |
| --- | --- |
| D200 is missing or reconnecting | USB cable, port, `d200-error.log`, and `--diagnose` |
| Task images appear but switching fails | Whether Codex handles `codex://` links |
| Pin, New, or Steer does nothing | Accessibility permission and whether the shortcut works directly in Codex |
| Theme changes do not appear | Reset the display digest as described in [Configuration](configuration.md#theme) |
| Usage is temporarily empty | Wait for initial app-server data and check the Codex account connection |

## Update

Pull the latest source, run the checks, and reinstall:

```bash
npm run check
npm run setup
```

## Uninstall

```bash
npm run uninstall
```

This stops and removes openCodexMicro and any recognized legacy
CodexKeyboard services, then deletes both runtime directories. Back up a
custom theme before uninstalling.

## Development

Create a local venv so checks do not modify the system Python:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r standalone/requirements.txt
npm run check
```

Available commands:

```bash
npm test       # Python unit tests
npm run check  # Unit tests and Node.js syntax checks
```

Main entry points:

- `standalone/d200.py`: HID, profiles, input path, and display queue;
- `standalone/native_codex.py`: task state, Usage, deep links, and shortcuts;
- `scripts/install.mjs`: installation and legacy migration;
- `scripts/uninstall.mjs`: service and runtime removal.
