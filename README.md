# openCodexMicro

**Turn your Ulanzi D200 into Codex Micro — plus the controls that make it a
complete Codex Desktop companion.**

[中文说明](README_zh.md)

![Meet OpenCodexMicro](docs/images/opencodexmicro-promo-comic-v2.png)

openCodexMicro gives Codex Desktop a dedicated hardware surface. See what your
latest tasks are doing, jump between them with one press, and keep the actions
you use most under your fingertips.

![openCodexMicro on an Ulanzi D200](docs/images/codex-keyboard-hero.png)

## What it does

| Feature | Behavior |
| --- | --- |
| Five live task keys | Merge local and Codex-managed SSH tasks in Most Recent order, with idle, thinking, complete, input/approval, or error |
| Instant task switching | Opens the exact task shown on the physical key |
| Codex controls | Fast, Pin, New, Fork, Steer, Mic, and Submit |
| Usage at a glance | Shows the remaining weekly allowance and refreshes it automatically |
| Clock and Focus | Keeps the D200 firmware clock and uses it as a Codex focus key |
| Event-driven integration | Uses Codex app-server, rollout events, persistent SSH, plus an optional loopback-only official-Micro bridge |
| Responsive display | Sends only changed keys and keeps input ahead of display transfers |
| Hot-plug recovery | Reconnects to the D200 and restores its last known display |

![openCodexMicro flat key layout](docs/images/open-codex-micro-layout.png)

![openCodexMicro in a desktop workspace](docs/images/codex-keyboard-workspace.png)

This is an unofficial project for **macOS, Codex Desktop, and Ulanzi D200**.
The entire project was vibe-coded with Codex.

## Install

Requirements:

- macOS with Codex Desktop installed
- Ulanzi D200 connected over USB
- Node.js 20+
- Python 3.11+ with `venv`

Clone the repository, then run:

```bash
npm install
npm run setup
```

The installer creates an isolated Python environment, installs `hidapi` and
Pillow, registers the D200 and bridge sidecar LaunchAgents, and installs
`Codex Bridge.app` in `~/Applications`. Existing CodexKeyboard installations are migrated
automatically; a legacy custom theme is copied before the old runtime is
removed.

For fast direct switching, quit Codex and double-click **Codex Bridge.app**.
It starts the real Codex executable with a loopback-only CDP endpoint. Task
keys then use Codex's own Micro event bus, including its saved SSH
host/project routing. The official Micro page reports the emulated device as
connected.

If Codex was launched normally, the first task press explains that Bridge mode
is inactive. Local tasks use `codex://threads/<id>`; SSH tasks use Codex's
native Dock **Recent** callback by exact title. The Dock menu can appear
briefly. This fallback never moves the pointer or depends on screen
coordinates, but it requires macOS Accessibility permission.

The installer also adds
`realtimeVoice.toggleMicrophoneMute → Command+Alt+M` when that Codex shortcut
is absent; an existing user override is preserved.

See [Setup and operations](docs/setup-and-operations.md) for permissions,
logs, diagnostics, updates, migration, and uninstall instructions.

## Configure

openCodexMicro follows Codex Desktop's own shortcuts instead of maintaining a
second shortcut system.

Shortcut overrides:

```text
~/.codex/keybindings.json
```

Submit and Steer behavior:

```text
~/.codex/config.toml
```

Theme overrides:

```text
~/Library/Application Support/openCodexMicro/icon-theme.json
```

Shortcut and Desktop settings are read when a key is pressed, so they normally
do not require a driver restart. Theme changes require a display refresh.

See [Configuration](docs/configuration.md) for supported commands, shortcut
syntax, Submit/Steer behavior, physical key remapping, and theme options.

The repository also includes reusable Codex skills:

| Skill | Purpose |
| --- | --- |
| [`install-open-codex-micro`](skills/install-open-codex-micro/SKILL.md) | Install or update, confirm shortcut changes, review permissions, and choose whether to start the daemon |
| [`customize-open-codex-micro-icons`](skills/customize-open-codex-micro-icons/SKILL.md) | Replace or generate task and action icons |
| [`remap-open-codex-micro-keys`](skills/remap-open-codex-micro-keys/SKILL.md) | Move existing controls or implement a new key action |

## Documentation

- [Configuration](docs/configuration.md)
- [Setup and operations](docs/setup-and-operations.md)
- [Architecture](docs/architecture.md)
- [D200 protocol notes](docs/d200-standalone.md)
- [Engineering constraints](docs/errors.md)

## License

Released under the [MIT License](LICENSE). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party attributions.
