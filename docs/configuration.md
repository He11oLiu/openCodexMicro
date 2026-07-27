# Configuration

openCodexMicro uses Codex Desktop's configuration for software shortcuts and
keeps device-specific visuals in a separate theme file.

## Action mapping

| D200 key | Function | Native action |
| --- | --- | --- |
| 1–5 | Recent tasks | Bridge: Codex Micro bus; normal: local deep link or SSH Dock Recent |
| 6 | Fast | `composer.toggleFastMode` |
| 7 | Usage / Focus | Focus Codex |
| 8 | Pin | `toggleThreadPin` |
| 9 | New | `newTask` |
| 10 | Fork | `forkThread` |
| 11 | Steer | Bridge invokes the visible composer's real Steer action |
| 12 | Mic | Bridge Micro `ACT10` down/up; configured shortcut fallback |
| 13 | Submit | `composer.submit` |
| 14 | Clock / Focus | Focus Codex |

Changing a shortcut affects behavior only; it does not change the icon shown
on the D200.

## Codex shortcut overrides

Codex Desktop stores shortcut overrides in:

```text
~/.codex/keybindings.json
```

The installer adds the Mic entry when it is missing and preserves an existing
override. Merge any other entries into the existing JSON array:

```json
[
  {
    "command": "composer.toggleFastMode",
    "key": "Command+Alt+T"
  },
  {
    "command": "toggleThreadPin",
    "key": "Command+Alt+P"
  },
  {
    "command": "newTask",
    "key": "Command+Shift+N"
  },
  {
    "command": "forkThread",
    "key": "Command+Alt+F"
  },
  {
    "command": "realtimeVoice.toggleMicrophoneMute",
    "key": "Command+Alt+M"
  },
  {
    "command": "composer.submit",
    "key": "Command+Enter"
  }
]
```

| D200 action | Codex command | Fallback |
| --- | --- | --- |
| Fast | `composer.toggleFastMode` | `Command+Alt+T` |
| Pin | `toggleThreadPin` | `Command+Alt+P` |
| New | `newTask` | `Command+N` |
| Fork | `forkThread` | `Command+Alt+F` |
| Mic | `realtimeVoice.toggleMicrophoneMute` | `Command+Alt+M` |
| Submit | `composer.submit` | Derived from Enter behavior |

The AppleScript translator supports:

- modifiers: `Command`, `Cmd`, `CmdOrCtrl`, `Alt`, `Option`, `Ctrl`,
  `Control`, and `Shift`;
- main keys: one character, `Enter`, or `Return`;
- combinations such as `Command+Alt+T`, `Command+Shift+N`, and
  `Command+Enter`.

Arrow keys, function keys, multi-character main keys, and unknown modifiers
are rejected. An invalid override falls back to the action's default.

The file is read for every action, so restarting openCodexMicro is normally
not required. Test the shortcut in Codex Desktop before testing the physical
key.

## Submit and Steer

Submit follows the Desktop section in:

```text
~/.codex/config.toml
```

```toml
[desktop]
followUpQueueMode = "queue"
composerEnterBehavior = "enter"
```

Snake-case names are also accepted:

```toml
[desktop]
follow_up_queue_mode = "queue"
composer_enter_behavior = "enter"
```

Supported `composerEnterBehavior` values are `enter`, `cmdIfMultiline`, and
`cmdAlways`.

`composer.submit` in `keybindings.json` takes priority for Submit. Without an
override, Submit uses `Enter` when `composerEnterBehavior` is `enter`, and
`Command+Enter` otherwise.

In Bridge mode, Steer focuses the visible composer and invokes Codex's actual
React **Steer** action. This keeps Codex's internal local/remote host routing
and avoids the unreliable application-level focus used by older versions.
It intentionally does not fall back to an Enter shortcut: on current Codex
versions that can submit or queue the draft instead of steering it. Launch
Codex through `Codex Bridge.app` to use the physical Steer key.

When Codex is running through `Codex Bridge.app`, Mic uses the official Codex
Micro double-key HID slot (`ACT10_ACT11`, dispatched through physical
`ACT10` down/up events). The configured `realtimeVoice.toggleMicrophoneMute`
shortcut remains the fallback when the bridge is unavailable.

Shortcut settings are read when a key is pressed and do not require a restart.

## Physical key layout

Physical positions use zero-based D200 indices in `standalone/d200.py`:

```python
ACTION_KEYS = {
    5: "fast",
    7: "pin",
    8: "new",
    9: "fork",
    10: "steer",
    11: "mic",
    12: "submit",
}
USAGE_DISPLAY_KEY = 6
FOCUS_KEY = 13
```

After moving existing actions, run the checks and reinstall:

```bash
npm run check
npm run setup
```

Adding a new action also requires updates to:

- `dispatch_desktop_action()` in `standalone/native_codex.py`;
- `surfaces` in `standalone/icon-theme.default.json`;
- the corresponding tests.

## Theme

The packaged theme is defined by:

```text
standalone/icon-theme.default.json
```

Runtime-ready 196×196 PNG files are in
`standalone/assets/generated/runtime/`. Full-resolution generation sources and
unused candidates are local authoring inputs and are excluded from the public
release snapshot.

After installation, place overrides in:

```text
~/Library/Application Support/openCodexMicro/icon-theme.json
```

Example:

```json
{
  "overridesOnly": true,
  "surfaces": {
    "fast": "/Users/your-name/Pictures/d200-fast.png"
  },
  "tasks": {
    "thinking": "/Users/your-name/Pictures/d200-thinking.png"
  },
  "usage": {
    "high": "#24c487",
    "medium": "#efa13b",
    "low": "#eb5d6c",
    "track": "#d2d7d6",
    "text": "#303638",
    "strokeWidth": 10,
    "fontSize": 36,
    "percentFontSize": 18
  }
}
```

Supported overrides:

- `tasks`: `idle`, `thinking`, `complete`, `input`, and `error`;
- `surfaces`: `fast`, `usage`, `pin`, `new`, `fork`, `steer`, `mic`, and
  `submit`;
- `usage`: ring, track, and text colors, stroke width, and font sizes.

Relative image paths are resolved from the directory containing
`icon-theme.json`. Task images are quantized to 128 colors and action images
to 96 colors.

The installer preserves every valid JSON theme, including themes created by
older releases. Invalid themes are backed up before a clean template is
created.

The theme is loaded when the driver starts. To force a new display profile,
move the old display digest aside and restart the driver:

```bash
app_root="$HOME/Library/Application Support/openCodexMicro"
if [[ -f "$app_root/d200-profile-cache.json" ]]; then
  mv "$app_root/d200-profile-cache.json" "$app_root/d200-profile-cache.json.bak"
fi
launchctl kickstart -k "gui/$(id -u)/io.opencodexmicro.d200"
```

Delete the backup after confirming the new theme works.
