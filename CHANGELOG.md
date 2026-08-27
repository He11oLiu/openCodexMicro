# Changelog

## 0.3.2 — 2026-08-27

### Fixed

- Generate the Bridge LaunchAgent with `CODEX_KEYBOARD_NODE` when configured,
  otherwise preferring Homebrew's stable `/opt/homebrew/bin/node` or
  `/usr/local/bin/node` symlink instead of a versioned Cellar executable. Skip
  stable candidates that do not meet the existing Node version requirement.
- Treat CDP usage as available only after a rate-limit payload and its windows
  validate. Probe compatible query keys and camelCase/snake_case payloads, and
  merge native app-server usage when renderer usage is unavailable without
  giving up the renderer's slots or connected state. Do not render stale usage
  when the selected provider reports that its data is unavailable. Reject
  payloads without a supported duration or weekly window so native fallback
  can take over. Display the current weekly remainder derived from OpenAI's
  current `usedPercent`; do not predict a future period.
- Expire the cached renderer Micro slot source after two seconds. Rediscovery
  retains the six-slot/id validation while making concurrent agent starts,
  completions, and switches visible without restarting Codex or the Bridge.
- Reuse the Micro event bus found during Bridge enablement when taking the
  first slot snapshot. If that cache is unavailable, probe only targeted
  Codex/Micro assets and stop at the first valid bus instead of importing every
  renderer asset. This removes the cold-start discovery stall while retaining
  a bounded compatibility fallback.
- Drop physical key-down commands that are still queued more than two seconds
  after capture, including agent actions waiting for an executor worker. Late
  key-up events are still delivered so a delayed release cannot leave a Micro
  action logically pressed. Failed, expired, and completed dispatches remain
  distinct in the daemon log.

### Compatibility

- Keep the 0.3.1 runtime floor: macOS 13 or newer, Node.js 20 or newer, and
  Python 3.11 or newer. The installer still accepts newer Node/Python releases
  and does not pin either runtime to one minor version.
- Continue accepting 0.3.1 Bridge payloads that predate `usageAvailable`, while
  treating an explicit `usageAvailable: false` as authoritative. Formal UUIDs
  and `client-new-thread:<uuid>` task keys remain supported end to end.

### Upgrade

- Run `npm install`, `npm run check`, and `npm run setup`. Re-running setup is
  required because this release replaces the installed bridge bundle and D200
  driver and regenerates the Bridge LaunchAgent with the stable Node path.
- Existing themes and shortcut overrides remain compatible. To enable direct
  Micro routing after installation, quit Codex and launch the regenerated
  `~/Applications/Codex Bridge.app`.

### Verification

- `npm run check` passes 91 Python tests, 17 Node tests, syntax validation, and
  the bundled Bridge build. The Python suite was verified on 3.11 and 3.14;
  the Node suite was verified on 20, 22, 24, and 25. `npm audit` reports zero
  known vulnerabilities.
- A live installation verified matching source/installed artifacts, both
  LaunchAgents running, six renderer slots, current weekly usage, stable
  two-second rediscovery, and responsive task keys during a complete D200
  profile refresh.

### Known behavior

- Every daemon start or USB reconnect treats the D200 framebuffer as unknown
  and sends one complete current profile before committing the new key mapping.
  The tested device took about 16 seconds for that full transfer; physical
  input continued to dispatch while it was in progress.

## 0.3.1 — 2026-07-31

### Changed

- Route Fast, Pin, New, Fork, Mic, Steer, and Submit through the renderer
  Bridge whenever it is the active state source. Fast/Fork/Submit/Mic preserve
  Micro press/release events; Pin/New invoke their semantic renderer controls.
- Suppress shortcut replay after uncertain Bridge failures so one-shot actions
  and toggles cannot execute twice.
- Wait up to 30 seconds for a cold Codex Bridge launch and keep the last Bridge
  source through five seconds of transient renderer failures, preventing a
  false fallback alert while the CDP endpoint is still coming online.
- On each D200 HID session, upload only the current full framebuffer. Remove
  the synchronous stale-profile replay and background reconnect-ZIP rebuild
  that doubled reconnect work, blocked button reads, and consumed idle CPU.

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
