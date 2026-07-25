---
name: install-open-codex-micro
description: Install, update, migrate, diagnose, or uninstall openCodexMicro on macOS. Use when Codex should prepare the Ulanzi D200 native daemon, prevent duplicate daemon instances, check dependencies and macOS permissions, optionally update Codex Desktop shortcuts, or decide whether to start the daemon after installation.
---

# Install openCodexMicro

Resolve the project root as two directories above this file. If `package.json`
and `standalone/d200.py` are not there, ask for the openCodexMicro checkout.

## Workflow

1. Inspect `README.md`, `package.json`, `git status --short`, and the installed
   service state. Check all three surfaces before changing anything:

   ```bash
   launchctl print "gui/$(id -u)" | rg -i -C 2 \
     'opencodexmicro|codexkeyboard|d200'
   find "$HOME/Library/LaunchAgents" -maxdepth 1 -type f \
     \( -iname '*opencodexmicro*.plist' -o \
        -iname '*codexkeyboard*.plist' -o -iname '*d200*.plist' \) -print
   ps -axo pid=,ppid=,command= | rg \
     '[Pp]ython.*(openCodexMicro|CodexKeyboard).*(d200\.py)|[Pp]ython.*d200\.py'
   ```

   Record every matching label, plist, PID, and executable path. Do not
   overwrite unrelated worktree changes.
2. Verify macOS, Node.js 20+, Python 3.11+ with `venv`, Codex Desktop, and the
   Ulanzi D200 connection.
3. Before editing `~/.codex/keybindings.json`, explicitly ask whether the user
   wants shortcut changes. If yes, ask for or confirm the desired mappings,
   preserve unrelated entries, and validate the final JSON. If no, leave it
   untouched.
4. Explicitly ask whether to start the daemon after installation.
5. Explain that action keys require:
   - System Settings → Privacy & Security → Accessibility for the installed
     Python process;
   - Automation access to `System Events` when macOS prompts;
   - USB access to the D200.
6. Before installation, ensure there is at most one recognized D200 daemon.
   The installer may replace the managed openCodexMicro or legacy
   CodexKeyboard LaunchAgent, but it must not run beside a manually started
   `d200.py`. If an unmanaged or ambiguous process exists, do not start another
   daemon; show its PID and command and ask before stopping it.
7. Install with:

   ```bash
   npm run setup
   ```

   If the user does not want the daemon started, run:

   ```bash
   npm run setup -- --no-start
   ```

8. If started, verify both launchd ownership and process cardinality:

   ```bash
   launchctl print "gui/$(id -u)/io.opencodexmicro.d200"
   ps -axo pid=,ppid=,command= | rg \
     '[Pp]ython.*(openCodexMicro|CodexKeyboard).*(d200\.py)|[Pp]ython.*d200\.py'
   find "$HOME/Library/LaunchAgents" -maxdepth 1 -type f \
     \( -iname '*opencodexmicro*.plist' -o \
        -iname '*codexkeyboard*.plist' -o -iname '*d200*.plist' \) -print
   tail -n 40 "$HOME/Library/Application Support/openCodexMicro/d200.log"
   tail -n 40 "$HOME/Library/Application Support/openCodexMicro/d200-error.log"
   ```

   Require exactly one matching `d200.py` process, owned by
   `io.opencodexmicro.d200`, and only its current plist. Treat a missing D200 as
   a device/USB issue, not a failed daemon install. A slow HID write warning
   alone does not indicate a duplicate daemon.
9. If daemon start was declined, verify that the managed job is not running.
10. Report whether shortcuts changed, whether the daemon started, its single
    PID, which permissions still need user action, and the verification result.

## Safety

- Do not modify shortcuts or start the daemon without the two confirmations.
- Never start a second daemon to test whether the first one is healthy.
- Never stop an unmanaged or ambiguous D200 process without user confirmation.
- Preserve valid user themes. The installer migrates the old CodexKeyboard
  theme and backs up invalid configuration.
- Do not delete runtime data manually; use `npm run uninstall` only when the
  user explicitly asks to uninstall.
- Never enable Chromium debugging or restore the removed bridge mode.
