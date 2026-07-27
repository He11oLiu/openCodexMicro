# Changelog

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
