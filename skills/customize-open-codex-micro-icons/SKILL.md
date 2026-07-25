---
name: customize-open-codex-micro-icons
description: Replace, generate, preview, and apply openCodexMicro task, action, or Usage icons for the Ulanzi D200. Use when a user asks to change the visual theme, create new 196×196 key art, update icon colors or symbols, or refresh customized icons on the device.
---

# Customize openCodexMicro Icons

Resolve the project root as two directories above this file. Read
`standalone/icon-theme.default.json` and `docs/configuration.md#theme` before
editing.

## Workflow

1. Ask which targets should change and whether the result is:
   - a local installed override, recommended for personal customization; or
   - a new repository default, appropriate for project-wide changes.
2. Inspect the current runtime and source assets under
   `standalone/assets/generated/`.
3. For newly generated bitmap art, use the available image-generation skill.
   Keep the high-resolution source under `source/`, then produce a 196×196 PNG
   under `runtime/`. Preserve the D200 key silhouette, generous padding,
   readable contrast, and the existing visual family.
4. Do not overwrite an existing asset unless the user explicitly approves it.
   Prefer a versioned filename.
5. Apply the result:
   - local override:
     `~/Library/Application Support/openCodexMicro/icon-theme.json`;
   - repository default: `standalone/icon-theme.default.json`.
6. Validate every configured asset exists and run:

   ```bash
   npm run check
   ```

7. Preview the final PNG. For an installed theme, clear only the display digest
   as documented in `docs/configuration.md#theme`.
8. Ask before restarting the daemon:

   ```bash
   launchctl kickstart -k "gui/$(id -u)/io.opencodexmicro.d200"
   ```

## Asset rules

- Task states: `idle`, `thinking`, `complete`, `input`, `error`.
- Action surfaces: `fast`, `usage`, `pin`, `new`, `fork`, `steer`, `mic`,
  `submit`.
- Use PNG at 196×196 for runtime assets.
- Task icons are quantized to 128 colors; action icons to 96 colors.
- Do not put task titles, personal information, watermarks, or secrets into
  images or metadata.
- Keep the firmware Clock/Focus surface image-free.
