# Remote navigation POCs

These probes compare ways to open a Codex SSH task.

Measured against `lite-shanghai / admin / hi`:

| Route | Result | Menu/pointer | Notes |
| --- | --- | --- | --- |
| Official Codex Micro event bus over loopback CDP | success | none | Fastest reliable route; uses saved thread → host/project assignment |
| Dock Recent native callback | success | menu briefly visible; pointer unchanged | Production fallback when Codex was launched normally |
| Codex App `navigate_to_codex_page` tool | success | none | Internal tool, unavailable to a standalone daemon |
| AX locator + synthetic click | success | pointer unchanged | Rejected for production because it is coordinate/click based |
| `Command+1…9` | wrong/missing task | none | Pinned tasks consume slots; not a global Most Recent mapping |
| WebView `AXPress` | silent no-op | none | Reports success without routing |
| `codex://threads/<id>?hostId=…` | failure for SSH | none | Deep-link handler queries the local app-server |
| `ipc.sock` App Action request | failure | none | No external renderer client is registered |

The bridge implementation lives in `src/bridge/`. It enables Codex's Micro
gate, finds the renderer event bus, emits a connected device state, then sends
`codex-micro-hid-event` with `local:<thread-id>`. The `local:` prefix is also
used for SSH tasks; Codex's saved assignment performs the remote routing.

Without a loopback CDP endpoint, `standalone/native_codex.py` falls back to the
Dock Recent callback for SSH tasks. It requires an exact unique title and
never uses mouse coordinates.
