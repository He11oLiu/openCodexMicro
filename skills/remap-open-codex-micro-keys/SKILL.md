---
name: remap-open-codex-micro-keys
description: Change openCodexMicro physical D200 key positions, replace existing actions, or add a new Codex action safely. Use when a user asks to remap buttons, swap Fast/Pin/New/Fork/Steer/Mic/Submit, move Usage or Focus, or implement a new hardware key function.
---

# Remap openCodexMicro Keys

Resolve the project root as two directories above this file. Read
`docs/configuration.md#physical-key-layout`, `standalone/d200.py`, and the
layout tests before editing.

## Decide the change

1. Confirm the desired physical mapping with the user.
2. Distinguish:
   - changing a Codex keyboard shortcut only;
   - moving an existing D200 action;
   - adding a new action.
3. If only the shortcut changes, edit Codex Desktop's
   `~/.codex/keybindings.json` after explicit user approval; do not change the
   physical map.

## Edit the physical map

- D200 indices are zero-based and laid out left-to-right, top-to-bottom.
- Keep indices `0..4` reserved for the five Most Recent task keys unless the
  user explicitly changes the product design.
- Keep exactly one Usage key and one Clock/Focus key.
- Prevent duplicate indices and ensure every action has a matching surface.
- Update `ACTION_KEYS`, `USAGE_DISPLAY_KEY`, and `FOCUS_KEY` in
  `standalone/d200.py`.
- For a new action, also update:
  - `dispatch_desktop_action()` in `standalone/native_codex.py`;
  - `standalone/icon-theme.default.json`;
  - the relevant tests and documentation.

## Validate and apply

1. Update the layout assertion in `test/standalone_test.py`.
2. Run:

   ```bash
   npm run check
   ```

3. Regenerate `docs/images/open-codex-micro-layout.png` when the documented
   layout changes:

   ```bash
   .venv/bin/python scripts/render-readme-layout.py
   ```

4. Ask whether the user wants to reinstall and start the daemon. Only after
   confirmation run:

   ```bash
   npm run setup
   ```

5. Remind the user that simulated shortcuts require Accessibility and
   `System Events` Automation permission.

Never reintroduce the removed bridge path.
