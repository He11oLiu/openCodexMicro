# Changelog

## Unreleased

### Changed

- Route Fast, Pin, New, Fork, Mic, Steer, and Submit through the renderer
  Bridge whenever it is the active state source. Fast/Fork/Submit/Mic preserve
  Micro press/release events; Pin/New invoke their semantic renderer controls.
- Suppress shortcut replay after uncertain Bridge failures so one-shot actions
  and toggles cannot execute twice.

## 0.3.0 — 2026-07-31

### Fixed

- Prevent a Bridge HTTP error from terminating the long-lived D200 action
  dispatcher; each action failure is now logged and isolated.
- Keep Bridge-mode Mic on the official Micro `ACT10` path even when one request
  fails, avoiding a duplicate shortcut toggle after an uncertain HTTP result.
- Treat every newly opened D200 HID session as an unknown framebuffer and send
  one complete profile, preventing stale disk digests from leaving keys blank.
- Accept formal UUIDs and explicit `client-new-thread:<uuid>` keys through the
  Python, encoded HTTP path, and CDP validation layers.
- Preserve remote diagnostic inventory when an old remote SQLite cannot parse
  Codex's partial-index schema, and degrade that monitor to rollout-only mode.

### Changed

- Use the renderer Micro store as the default D200 state source. Bridge loss
  activates a local-only native fallback; remote SSH/SQLite monitoring is now
  explicit diagnostic behavior.
- Refresh the Bridge cache every 500ms. Snapshot discovery caches its Micro
  store/resolver/context/query-client references, reducing hot snapshots from
  multi-second Fiber scans to approximately 0.2–1.2ms in local verification.

## 0.2.1 — 2026-07-27

### Fixed

- Force a complete 14-key profile refresh after a real D200 USB disconnect.
  The previous code trusted persisted frame and per-key digests after reconnect,
  so an interrupted or power-cycled device could receive only a sparse update
  and leave some keys stale.
- Route Steer through Codex's real visible-composer React action. Keyboard
  variants (`Enter`, `Command+Enter`, and `Command+Shift+Enter`) were dependent
  on Desktop settings and could submit or queue the draft instead of steering.
  A missing Steer action now stays a no-op rather than becoming Send.
- Route Mic through the official Codex Micro `ACT10` down/up events. The old
  shortcut-only path discarded the physical release phase and did not work
  reliably across Macs.
- Allow task-key Bridge navigation up to three seconds and log Bridge dispatch
  failures, avoiding a misleading normal-mode notice during a transient slow
  renderer response.

### Verification

- Confirmed a reconnect/version refresh uploaded a full 145,616-byte profile.
- Confirmed Steer inserted a `user_message` into the same active rollout turn
  with no intervening `task_complete` or `task_started`.
- Confirmed Mic down/up endpoints return successful Bridge responses.
- Added regression coverage for reconnect display baselines, Mic phases,
  renderer Steer routing, safe Steer failure, and Bridge navigation timeout.
