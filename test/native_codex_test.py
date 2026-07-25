import json
import queue
import select
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "standalone"))

from native_codex import (
    NativeCodex,
    RolloutTail,
    RolloutWatcher,
    composer_enter_behavior,
    configured_shortcuts,
    follow_up_mode,
    shortcut_script,
    steer_key_script,
    usage_windows,
)


class NativeCodexTests(unittest.TestCase):
    def test_steer_inverts_codex_queue_mode_without_manual_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[desktop]\nfollowUpQueueMode = "queue"\n')
            self.assertEqual(follow_up_mode(path), "queue")
            self.assertEqual(
                steer_key_script(path),
                "key code 36 using command down",
            )
            path.write_text('[desktop]\nfollowUpQueueMode = "steer"\n')
            self.assertEqual(steer_key_script(path), "key code 36 using command down")

    def test_steer_uses_shift_for_cmd_enter_send_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[desktop]\nfollowUpQueueMode = "queue"\n'
                'composerEnterBehavior = "cmdAlways"\n'
            )
            self.assertEqual(composer_enter_behavior(path), "cmdAlways")
            self.assertEqual(
                steer_key_script(path),
                "key code 36 using {command down, shift down}",
            )

    def test_reads_and_translates_codex_keybindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "keybindings.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "command": "composer.toggleFastMode",
                            "key": "Command+Alt+T",
                        }
                    ]
                )
            )
            self.assertEqual(
                configured_shortcuts(path)["composer.toggleFastMode"],
                "Command+Alt+T",
            )
            self.assertEqual(
                shortcut_script("Command+Alt+T"),
                'keystroke "t" using {command down, option down}',
            )

    def test_rejects_unknown_shortcut_modifiers_instead_of_dropping_them(self):
        with self.assertRaisesRegex(ValueError, "Hyper"):
            shortcut_script("Command+Hyper+T")

    def test_rollout_tail_tracks_incremental_task_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-24T14:24:54.733Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n"
            )
            tail = RolloutTail(path)
            self.assertTrue(tail.update())
            self.assertGreater(tail.started_at, tail.completed_at)

            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-24T14:25:12.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_complete"},
                        }
                    )
                    + "\n"
                )
            self.assertTrue(tail.update())
            self.assertGreater(tail.completed_at, tail.started_at)

    def test_rollout_tail_tracks_real_approval_until_matching_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            call_id = "call-needs-approval"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:40:44.327Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": call_id,
                            "arguments": json.dumps(
                                {
                                    "sandbox_permissions": "require_escalated",
                                    "justification": "Allow this action?",
                                }
                            ),
                        },
                    }
                )
                + "\n"
            )
            tail = RolloutTail(path)
            self.assertTrue(tail.update())
            self.assertEqual(tail.pending_input, {call_id})

            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-25T00:48:09.417Z",
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": "approved and completed",
                            },
                        }
                    )
                    + "\n"
                )
            self.assertTrue(tail.update())
            self.assertFalse(tail.pending_input)

    def test_rollout_watcher_uses_kernel_file_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("")
            events: queue.Queue = queue.Queue()
            watcher = RolloutWatcher(
                lambda changed, is_directory, flags: events.put(
                    (changed, is_directory, flags)
                )
            )
            try:
                watcher.configure([path])
                with path.open("a") as output:
                    output.write('{"type":"event_msg"}\n')
                    output.flush()
                deadline = time.monotonic() + 2
                matched = None
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if event[0] == path.resolve() and not event[1]:
                        matched = event
                        break
                self.assertIsNotNone(matched)
            finally:
                watcher.close()

    def test_rollout_watcher_reconfigures_incrementally(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "rollout-first.jsonl"
            second = Path(directory) / "rollout-second.jsonl"
            first.write_text("")
            second.write_text("")
            events: queue.Queue = queue.Queue()
            watcher = RolloutWatcher(
                lambda changed, is_directory, flags: events.put(
                    (changed, is_directory, flags)
                )
            )
            try:
                watcher.configure([first])
                watcher.configure([first, second])
                for path in (first, second):
                    with path.open("a") as output:
                        output.write('{"type":"event_msg"}\n')
                        output.flush()
                seen = set()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(seen) < 2:
                    try:
                        path, is_directory, _ = events.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if not is_directory:
                        seen.add(path)
                self.assertEqual(seen, {first.resolve(), second.resolve()})
            finally:
                watcher.close()

    def test_new_user_rollout_promotes_one_complete_logical_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing_path = root / (
                "rollout-2026-07-25T00-00-00-"
                "00000000-0000-0000-0000-000000000001.jsonl"
            )
            existing_path.write_text("")
            new_path = root / (
                "rollout-2026-07-25T00-00-01-"
                "00000000-0000-0000-0000-000000000002.jsonl"
            )
            new_path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:01.000Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "00000000-0000-0000-0000-000000000002",
                            "thread_source": "user",
                        },
                    }
                )
                + "\n"
                + ("x" * (2 * 1024 * 1024 + 100))
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:02.000Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n"
            )

            class Watcher:
                def configure(self, files):
                    self.files = files

            adapter = NativeCodex(start=False)
            adapter._first_run = False
            adapter._threads = [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "name": "Existing",
                    "path": str(existing_path),
                    "status": {"type": "notLoaded"},
                }
            ]
            adapter._tails[str(existing_path.resolve())] = RolloutTail(
                existing_path
            )
            adapter._watcher = Watcher()
            self.assertTrue(adapter._discover_rollouts(root))
            self.assertEqual(adapter._revision, 0)
            self.assertEqual(
                adapter._threads[0]["id"],
                "00000000-0000-0000-0000-000000000002",
            )
            adapter._publish()
            self.assertEqual(adapter._revision, 1)
            self.assertEqual(adapter.snapshot()["slots"][0]["status"], "thinking")
            adapter._publish()
            self.assertEqual(adapter._revision, 1)

    def test_new_subagent_rollout_is_not_promoted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (
                "rollout-2026-07-25T00-00-01-"
                "00000000-0000-0000-0000-000000000003.jsonl"
            )
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:01.000Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "00000000-0000-0000-0000-000000000003",
                            "thread_source": "subagent",
                        },
                    }
                )
                + "\n"
            )
            adapter = NativeCodex(start=False)
            self.assertFalse(adapter._discover_rollouts(Path(directory)))
            self.assertFalse(adapter._threads)
            self.assertIn(str(path.resolve()), adapter._ignored_rollouts)

    def test_new_rollout_event_commits_one_frame_without_waiting_for_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing_path = root / (
                "rollout-2026-07-25T00-00-00-"
                "00000000-0000-0000-0000-000000000001.jsonl"
            )
            existing_path.write_text("")
            existing = {
                "id": "00000000-0000-0000-0000-000000000001",
                "name": "Existing",
                "path": str(existing_path),
                "status": {"type": "notLoaded"},
            }

            class Client:
                def __init__(self, *_):
                    pass

                def on_notification(self, callback):
                    self.callback = callback

                def request(self, method, params=None):
                    if method == "thread/list":
                        return {"data": [existing]}
                    return {"rateLimits": {}}

                def close(self):
                    pass

            class Watcher:
                def __init__(self, callback):
                    self.callback = callback

                def configure(self, files):
                    self.files = files

                def close(self):
                    pass

            with (
                patch("native_codex.AppServerClient", Client),
                patch("native_codex.RolloutWatcher", Watcher),
                patch.object(
                    NativeCodex,
                    "_load_seen",
                    return_value={"existing": 0},
                ),
            ):
                adapter = NativeCodex()
                try:
                    revision, _ = adapter.wait_for_change(0, timeout=1)
                    self.assertGreaterEqual(revision, 1)
                    baseline_revision = revision
                    new_path = root / (
                        "rollout-2026-07-25T00-00-01-"
                        "00000000-0000-0000-0000-000000000004.jsonl"
                    )
                    new_path.write_text("")
                    started = time.monotonic()
                    adapter._on_rollout_event(
                        root,
                        True,
                        select.KQ_NOTE_WRITE,
                    )
                    # macOS may report creation before Codex flushes session_meta.
                    time.sleep(0.02)
                    new_path.write_text(
                        json.dumps(
                            {
                                "timestamp": "2026-07-25T00:00:01.000Z",
                                "type": "session_meta",
                                "payload": {
                                    "id": (
                                        "00000000-0000-0000-0000-"
                                        "000000000004"
                                    ),
                                    "thread_source": "user",
                                },
                            }
                        )
                        + "\n"
                        + json.dumps(
                            {
                                "timestamp": "2026-07-25T00:00:02.000Z",
                                "type": "event_msg",
                                "payload": {"type": "task_started"},
                            }
                        )
                        + "\n"
                    )
                    revision, state = adapter.wait_for_change(
                        baseline_revision,
                        timeout=1,
                    )
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertEqual(revision, baseline_revision + 1)
                    self.assertEqual(
                        state["slots"][0]["threadKey"],
                        "00000000-0000-0000-0000-000000000004",
                    )
                    self.assertEqual(state["slots"][0]["status"], "thinking")
                    time.sleep(0.15)
                    self.assertEqual(adapter._revision, revision)
                finally:
                    adapter.close()

    def test_app_server_status_notification_publishes_immediately(self):
        adapter = NativeCodex(start=False)
        adapter._threads = [
            {
                "id": "thread-1",
                "name": "Needs approval",
                "path": None,
                "status": {"type": "idle"},
            }
        ]
        changed, inventory_changed = adapter._handle_notification(
            {
                "method": "thread/status/changed",
                "params": {
                    "threadId": "thread-1",
                    "status": {
                        "type": "active",
                        "activeFlags": ["waitingOnApproval"],
                    },
                },
            }
        )
        self.assertTrue(changed)
        self.assertFalse(inventory_changed)
        adapter._publish()
        revision, state = adapter.wait_for_change(0, timeout=0)
        self.assertEqual(revision, 1)
        self.assertEqual(state["slots"][0]["status"], "input")

    def test_startup_rate_limit_notification_does_not_publish_second_frame(self):
        adapter = NativeCodex(start=False)
        adapter._last_usage_refresh = time.monotonic()
        with patch.object(
            adapter,
            "_refresh_rate_limits",
            side_effect=AssertionError("startup burst should be coalesced"),
        ):
            changed, inventory_changed = adapter._handle_notification(
                {
                    "method": "account/rateLimits/updated",
                    "params": {},
                }
            )
        self.assertFalse(changed)
        self.assertFalse(inventory_changed)

    def test_rollout_append_changes_state_without_inventory_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("")
            adapter = NativeCodex(start=False)
            adapter._tails[str(path.resolve())] = RolloutTail(path)
            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-25T00:00:00.000Z",
                            "type": "event_msg",
                            "payload": {"type": "task_started"},
                        }
                    )
                    + "\n"
                )
            changed, inventory_changed = adapter._handle_filesystem(
                path,
                False,
                select.KQ_NOTE_WRITE,
            )
            self.assertTrue(changed)
            self.assertTrue(inventory_changed)

    def test_rollout_approval_publishes_input_then_returns_to_thinking(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text("")
            adapter = NativeCodex(start=False)
            adapter._threads = [
                {
                    "id": "thread-1",
                    "name": "Approve",
                    "path": str(path),
                    "status": {"type": "notLoaded"},
                }
            ]
            tail = RolloutTail(path)
            tail.started_at = 100
            adapter._tails[str(path.resolve())] = tail
            call_id = "call-1"
            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-25T00:40:44.327Z",
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "name": "exec_command",
                                "call_id": call_id,
                                "arguments": json.dumps(
                                    {
                                        "sandbox_permissions": "require_escalated"
                                    }
                                ),
                            },
                        }
                    )
                    + "\n"
                )
            changed, _ = adapter._handle_filesystem(
                path,
                False,
                select.KQ_NOTE_WRITE,
            )
            self.assertTrue(changed)
            adapter._publish()
            self.assertEqual(adapter.snapshot()["slots"][0]["status"], "input")

            with path.open("a") as output:
                output.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-25T00:48:09.417Z",
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": "done",
                            },
                        }
                    )
                    + "\n"
                )
            adapter._handle_filesystem(path, False, select.KQ_NOTE_WRITE)
            adapter._publish()
            self.assertEqual(
                adapter.snapshot()["slots"][0]["status"],
                "thinking",
            )

    def test_rollout_failure_publishes_error_without_app_server_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:01.000Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_aborted",
                            "reason": "system_error",
                        },
                    }
                )
                + "\n"
            )
            adapter = NativeCodex(start=False)
            adapter._threads = [
                {
                    "id": "thread-1",
                    "name": "Failed",
                    "path": str(path),
                    "status": {"type": "notLoaded"},
                }
            ]
            tail = RolloutTail(path)
            self.assertTrue(tail.update())
            adapter._tails[str(path.resolve())] = tail
            adapter._publish()
            self.assertEqual(
                adapter.snapshot()["slots"][0]["status"],
                "error",
            )

    def test_known_turn_notification_does_not_refresh_inventory(self):
        adapter = NativeCodex(start=False)
        adapter._threads = [
            {
                "id": "thread-1",
                "name": "Build",
                "path": None,
                "status": {"type": "idle"},
            }
        ]
        changed, inventory_changed = adapter._handle_notification(
            {
                "method": "turn/started",
                "params": {"threadId": "thread-1"},
            }
        )
        self.assertTrue(changed)
        self.assertFalse(inventory_changed)

    def test_inventory_watches_background_threads_for_recent_promotion(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            threads = []
            for index in range(6):
                path = Path(directory) / f"rollout-{index}.jsonl"
                path.write_text("")
                paths.append(path)
                threads.append(
                    {
                        "id": f"thread-{index}",
                        "name": f"Task {index}",
                        "path": str(path),
                        "status": {"type": "notLoaded"},
                    }
                )

            class Client:
                def request(self, method, params=None):
                    self.method = method
                    self.params = params
                    return {"data": [threads[5], *threads[:5]]}

            class Watcher:
                def configure(self, files):
                    self.files = files

            adapter = NativeCodex(start=False)
            adapter._first_run = False
            adapter._client = Client()
            adapter._watcher = Watcher()
            adapter._refresh_inventory()
            self.assertEqual(
                [thread["id"] for thread in adapter._threads],
                ["thread-5", "thread-0", "thread-1", "thread-2", "thread-3"],
            )
            self.assertEqual(
                set(adapter._watcher.files),
                {path.resolve() for path in paths},
            )
            self.assertEqual(
                adapter._client.params["limit"],
                2048,
            )
            self.assertEqual(adapter._client.params["sortKey"], "updated_at")

    def test_inventory_catches_write_during_watcher_reconfigure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:00.000Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                )
                + "\n"
            )
            thread = {
                "id": "thread-1",
                "name": "Racing",
                "path": str(path),
                "status": {"type": "notLoaded"},
            }

            class Client:
                def request(self, method, params=None):
                    return {"data": [thread]}

            class Watcher:
                def configure(self, files):
                    with path.open("a") as output:
                        output.write(
                            json.dumps(
                                {
                                    "timestamp": "2026-07-25T00:00:01.000Z",
                                    "type": "event_msg",
                                    "payload": {"type": "task_complete"},
                                }
                            )
                            + "\n"
                        )

            adapter = NativeCodex(start=False)
            adapter._first_run = False
            adapter._client = Client()
            adapter._watcher = Watcher()
            self.assertTrue(adapter._refresh_inventory())
            tail = adapter._tails[str(path.resolve())]
            self.assertGreater(tail.completed_at, tail.started_at)

    def test_usage_converts_used_percent_to_remaining_windows(self):
        result = usage_windows(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 20,
                        "windowDurationMins": 300,
                        "resetsAt": 1,
                    },
                    "secondary": {
                        "usedPercent": 90,
                        "windowDurationMins": 10080,
                        "resetsAt": 2,
                    },
                }
            }
        )
        self.assertEqual(
            result,
            [
                {"kind": "five-hour", "remainingPercent": 80, "resetsAt": 1},
                {"kind": "weekly", "remainingPercent": 10, "resetsAt": 2},
            ],
        )


if __name__ == "__main__":
    unittest.main()
