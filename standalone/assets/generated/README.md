# Runtime icon library

The public release contains only the optimized 196×196 transparent PNGs under
`runtime/`. These files are referenced by `icon-theme.default.json` and copied
into the installed runtime.

Full-resolution generations and unused candidates are local authoring inputs.
They are excluded from release snapshots to keep clones small. New artwork can
be generated with the `customize-open-codex-micro-icons` skill.

## Active task assets

| State | Runtime asset |
| --- | --- |
| Idle | `runtime/task-idle-v2.png` |
| Working | `runtime/task-thinking-v2.png` |
| Complete | `runtime/task-complete-v3.png` |
| Input | `runtime/task-input-v2.png` |
| Error | `runtime/task-error-v2.png` |
