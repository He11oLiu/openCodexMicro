import base64
import io
import json
import os
import queue
import select
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "standalone"))

from native_codex import (
    CodexStateAdapter,
    NativeCodex,
    RolloutTail,
    RolloutWatcher,
    composer_enter_behavior,
    configured_shortcuts,
    dispatch_bridge_action,
    dispatch_bridge_thread,
    remote_hosts_from_state,
    shortcut_script,
    usage_windows,
)


class NativeCodexTests(unittest.TestCase):
    def test_bridge_state_is_authoritative_and_preserves_temporary_keys(self):
        adapter = CodexStateAdapter(start=False)
        temporary = "local:client-new-thread:f6805b8a-332a-43a0-a118-52d3e59542f6"
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": temporary,
                "title": "Pending task",
                "status": "working",
                "selected": True,
            }],
            "usage": {"windows": [{"kind": "weekly", "remainingPercent": 70}]},
            "usageSource": "cdp",
            "usageAvailable": True,
            "updatedAt": 123,
        }
        with patch("native_codex.fetch_bridge_state", return_value=payload), patch(
            "native_codex.NativeCodex"
        ) as native:
            adapter._poll_once()
        state = adapter.snapshot()
        self.assertEqual(state["source"], "bridge")
        self.assertEqual(state["slots"][0]["threadKey"], temporary)
        native.assert_not_called()

    def test_missing_cdp_usage_uses_native_usage_without_losing_bridge_slots(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": True,
            "source": "native",
            "slots": [{"threadKey": "native-slot"}],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "updatedAt": 456,
            },
            "error": None,
        }
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": "local:bridge-slot",
                "title": "Bridge task",
                "status": "working",
            }],
            "usage": None,
            "usageSource": "cdp",
            "usageAvailable": False,
            "updatedAt": 123,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch(
            "native_codex.NativeCodex", return_value=fallback
        ) as native:
            adapter._poll_once()
        state = adapter.snapshot()
        native.assert_called_once_with(enable_remote=False)
        self.assertEqual(state["source"], "bridge")
        self.assertEqual(state["slots"][0]["threadKey"], "local:bridge-slot")
        self.assertEqual(state["usageSource"], "native")
        self.assertTrue(state["usageAvailable"])
        self.assertEqual(state["usage"]["windows"][0]["remainingPercent"], 61)
        adapter.close()

    def test_five_hour_only_cdp_usage_uses_native_weekly_fallback(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": True,
            "source": "native",
            "slots": [],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "updatedAt": 456,
            },
            "usageSource": "native",
            "usageAvailable": True,
            "error": None,
        }
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": "local:bridge-slot",
                "title": "Bridge task",
                "status": "working",
            }],
            "usage": {
                "windows": [{"kind": "five-hour", "remainingPercent": 80}],
                "observedAt": 123,
            },
            "usageSource": "cdp",
            "usageAvailable": True,
            "updatedAt": 123,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch(
            "native_codex.NativeCodex", return_value=fallback
        ) as native:
            adapter._poll_once()
        state = adapter.snapshot()
        native.assert_called_once_with(enable_remote=False)
        self.assertEqual(state["slots"][0]["threadKey"], "local:bridge-slot")
        self.assertEqual(state["usageSource"], "native")
        self.assertTrue(state["usageAvailable"])
        self.assertEqual(state["usage"]["windows"][0]["kind"], "weekly")
        adapter.close()

    def test_legacy_bridge_weekly_usage_without_availability_flag_is_valid(self):
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": "local:bridge-slot",
                "title": "Bridge task",
                "status": "working",
            }],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "observedAt": 123,
            },
            "updatedAt": 123,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch("native_codex.NativeCodex") as native:
            adapter._poll_once()
        state = adapter.snapshot()
        native.assert_not_called()
        self.assertEqual(state["usageSource"], "cdp")
        self.assertTrue(state["usageAvailable"])
        self.assertEqual(state["usage"]["windows"][0]["kind"], "weekly")
        adapter.close()

    def test_explicitly_unavailable_bridge_usage_still_uses_native(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": False,
            "usage": {"windows": []},
            "error": "app-server unavailable",
        }
        payload = {
            "connected": True,
            "slots": [],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "observedAt": 123,
            },
            "usageAvailable": False,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch(
            "native_codex.NativeCodex", return_value=fallback
        ) as native:
            adapter._poll_once()
        state = adapter.snapshot()
        native.assert_called_once_with(enable_remote=False)
        self.assertFalse(state["usageAvailable"])
        self.assertEqual(state["usage"], {"windows": []})
        adapter.close()

    def test_malformed_bridge_windows_keep_slots_and_use_native_usage(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": True,
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "updatedAt": 456,
            },
            "error": None,
        }
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": "local:bridge-slot",
                "title": "Bridge task",
                "status": "working",
            }],
            "usage": {"windows": 42},
            "usageAvailable": True,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch(
            "native_codex.NativeCodex", return_value=fallback
        ):
            adapter._poll_once()
        state = adapter.snapshot()
        self.assertEqual(state["slots"][0]["threadKey"], "local:bridge-slot")
        self.assertEqual(state["usageSource"], "native")
        self.assertTrue(state["usageAvailable"])
        adapter.close()

    def test_boolean_bridge_remaining_percent_uses_native_usage(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": True,
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 61}],
                "updatedAt": 456,
            },
            "error": None,
        }
        payload = {
            "connected": True,
            "slots": [{
                "id": 0,
                "threadKey": "local:bridge-slot",
                "status": "working",
            }],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": True}]
            },
            "usageAvailable": True,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch(
            "native_codex.NativeCodex", return_value=fallback
        ):
            adapter._poll_once()
        state = adapter.snapshot()
        self.assertEqual(state["slots"][0]["threadKey"], "local:bridge-slot")
        self.assertEqual(state["usageSource"], "native")
        self.assertEqual(state["usage"]["windows"][0]["remainingPercent"], 61)
        adapter.close()

    def test_unavailable_native_usage_does_not_publish_stale_windows(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": False,
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 22}],
                "updatedAt": 456,
            },
            "error": "app-server unavailable",
        }
        payload = {
            "connected": True,
            "slots": [],
            "usage": None,
            "usageSource": "cdp",
            "usageAvailable": False,
        }
        adapter = CodexStateAdapter(start=False)
        with patch(
            "native_codex.fetch_bridge_state", return_value=payload
        ), patch("native_codex.NativeCodex", return_value=fallback):
            adapter._poll_once()
        state = adapter.snapshot()
        self.assertFalse(state["usageAvailable"])
        self.assertEqual(state["usage"], {"windows": []})
        adapter.close()

    def test_bridge_failure_uses_local_only_native_fallback(self):
        fallback = Mock()
        fallback.snapshot.return_value = {
            "connected": True,
            "source": "native",
            "slots": [{"threadKey": "local-only"}],
            "usage": {"windows": []},
            "error": None,
        }
        adapter = CodexStateAdapter(start=False)
        adapter._bridge_failure_since = (
            time.monotonic() - adapter.FALLBACK_AFTER_SECONDS
        )
        with patch(
            "native_codex.fetch_bridge_state",
            side_effect=URLError("offline"),
        ), patch("native_codex.NativeCodex", return_value=fallback) as native:
            adapter._poll_once()
        native.assert_called_once_with(enable_remote=False)
        self.assertEqual(adapter.snapshot()["source"], "native-local")
        adapter.close()
        fallback.close.assert_called_once()

    def test_transient_bridge_failures_do_not_publish_local_fallback(self):
        adapter = CodexStateAdapter(start=False)
        adapter._state["source"] = "bridge"
        with patch(
            "native_codex.fetch_bridge_state",
            side_effect=URLError("renderer reloading"),
        ), patch("native_codex.NativeCodex") as native:
            for _ in range(8):
                adapter._poll_once()
        self.assertEqual(adapter.snapshot()["source"], "bridge")
        native.assert_not_called()

    def test_bridge_recovery_closes_native_fallback(self):
        fallback = Mock()
        adapter = CodexStateAdapter(start=False)
        adapter._fallback = fallback
        adapter._state["source"] = "native-local"
        with patch(
            "native_codex.fetch_bridge_state",
            return_value={
                "connected": True,
                "slots": [],
                "usage": {
                    "windows": [{"kind": "weekly", "remainingPercent": 70}],
                    "observedAt": 123,
                },
                "usageSource": "cdp",
                "usageAvailable": True,
            },
        ):
            adapter._poll_once()
        self.assertEqual(adapter.snapshot()["source"], "bridge")
        self.assertIsNone(adapter._fallback)
        self.assertIsNone(adapter._bridge_failure_since)
        fallback.close.assert_called_once()

    def test_state_adapter_routes_the_displayed_temporary_slot(self):
        temporary = "local:client-new-thread:f6805b8a-332a-43a0-a118-52d3e59542f6"
        adapter = CodexStateAdapter(
            start=False,
            bridge_url="http://127.0.0.1:19000",
        )
        adapter._state["slots"] = [
            {"threadKey": "local:00000000-0000-0000-0000-000000000000"},
            {"threadKey": temporary},
        ]
        with patch(
            "native_codex.dispatch_bridge_thread",
            return_value=True,
        ) as bridge:
            adapter.open_thread(temporary)
        bridge.assert_called_once_with(
            temporary,
            1,
            bridge_url="http://127.0.0.1:19000",
        )

    def test_local_fallback_navigation_skips_known_unavailable_bridge(self):
        adapter = CodexStateAdapter(start=False)
        adapter._state["source"] = "native-local"
        thread_id = "f6805b8a-332a-43a0-a118-52d3e59542f6"
        with patch("native_codex.dispatch_bridge_thread") as bridge, patch(
            "native_codex.subprocess.Popen"
        ) as deep_link:
            adapter.open_thread(thread_id)
        bridge.assert_not_called()
        self.assertEqual(
            deep_link.call_args.args[0],
            ["/usr/bin/open", f"codex://threads/{thread_id}"],
        )

    def test_native_remote_monitoring_is_explicit(self):
        self.assertFalse(NativeCodex(start=False)._enable_remote)
        self.assertTrue(
            NativeCodex(start=False, enable_remote=True)._enable_remote
        )

    def test_ignores_app_server_notifications_outside_status_chain(self):
        adapter = NativeCodex(start=False)
        adapter._on_notification({
            "method": "item/agentMessage/delta",
            "params": {"delta": "high-frequency output"},
        })
        self.assertTrue(adapter._events.empty())
        message = {
            "method": "turn/started",
            "params": {"threadId": "thread-1"},
        }
        adapter._on_notification(message)
        self.assertEqual(
            adapter._events.get_nowait(),
            ("notification", message),
        )

    def test_bridge_thread_dispatch_bypasses_proxy_and_posts_slot(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true,"bridge":true}'
        opener = Mock()
        opener.open.return_value = response
        response.__enter__.return_value = response
        thread_id = "019f97a7-8c15-7942-8f48-c8ca32937ceb"
        with patch("native_codex.build_opener", return_value=opener) as build:
            self.assertTrue(dispatch_bridge_thread(thread_id, slot=3))
        self.assertEqual(build.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertTrue(request.full_url.endswith(f"/{thread_id}/click?slot=3"))
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3.0)

    def test_bridge_actions_bypass_proxy_and_preserve_phase(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.return_value = response
        response.__enter__.return_value = response
        with patch("native_codex.build_opener", return_value=opener) as build:
            for action in ("fast", "fork", "mic", "steer", "submit"):
                self.assertTrue(dispatch_bridge_action(action, pressed=False))
        self.assertEqual(build.call_args.args[0].proxies, {})
        requests = [call.args[0] for call in opener.open.call_args_list]
        self.assertTrue(all(request.method == "POST" for request in requests))
        self.assertEqual(
            [request.full_url.rsplit("/action/", 1)[1] for request in requests],
            [
                "fast/up",
                "fork/up",
                "mic/up",
                "steer/up",
                "submit/up",
            ],
        )

    def test_one_shot_renderer_action_release_is_a_local_noop(self):
        with patch("native_codex.build_opener") as opener:
            self.assertTrue(dispatch_bridge_action("pin", pressed=False))
            self.assertTrue(dispatch_bridge_action("new", pressed=False))
        opener.assert_not_called()

    def test_bridge_steer_invokes_the_renderer_action(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true}'
        opener = Mock()
        opener.open.return_value = response
        response.__enter__.return_value = response
        with patch("native_codex.build_opener", return_value=opener):
            self.assertTrue(dispatch_bridge_action("steer", pressed=True))
        request = opener.open.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/action/steer/down"))

    def test_bridge_http_error_is_logged_and_returned_as_failure(self):
        opener = Mock()
        error = HTTPError(
            "http://127.0.0.1:17373/action/steer/down",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"ok":false}'),
        )
        opener.open.side_effect = error
        try:
            with patch("native_codex.build_opener", return_value=opener), patch(
                "native_codex.sys.stderr"
            ):
                self.assertFalse(dispatch_bridge_action("steer", pressed=True))
        finally:
            error.close()

    def test_bridge_temporary_thread_key_is_encoded_as_one_path_segment(self):
        response = MagicMock()
        response.read.return_value = b'{"ok":true,"bridge":true}'
        opener = Mock()
        opener.open.return_value = response
        response.__enter__.return_value = response
        temporary = "client-new-thread:f6805b8a-332a-43a0-a118-52d3e59542f6"
        with patch("native_codex.build_opener", return_value=opener):
            self.assertTrue(dispatch_bridge_thread(temporary, slot=1))
        request = opener.open.call_args.args[0]
        self.assertIn(
            "/thread/client-new-thread%3Af6805b8a-332a-43a0-a118-52d3e59542f6/click",
            request.full_url,
        )

    def test_bridge_rejects_unrecognized_thread_keys(self):
        with patch("native_codex.build_opener") as opener:
            self.assertFalse(dispatch_bridge_thread("arbitrary-thread"))
        opener.assert_not_called()

    def test_bridge_steer_failure_never_falls_back_to_submit_shortcut(self):
        adapter = NativeCodex(start=False)
        with patch(
            "native_codex.dispatch_bridge_action",
            return_value=False,
        ), patch("native_codex.dispatch_desktop_action") as fallback:
            adapter.desktop_action("steer")
        fallback.assert_not_called()

    def test_bridge_mode_failure_does_not_duplicate_one_shot_actions(self):
        adapter = CodexStateAdapter(start=False)
        with patch("native_codex.dispatch_bridge_action", return_value=False) as bridge, patch(
            "native_codex.dispatch_desktop_action"
        ) as fallback:
            for action in ("fast", "pin", "new", "fork", "submit"):
                adapter.desktop_action(action, pressed=True)
        fallback.assert_not_called()

        self.assertEqual(
            [call.args[0] for call in bridge.call_args_list],
            ["fast", "pin", "new", "fork", "submit"],
        )

    def test_local_fallback_uses_shortcuts_only_on_key_down(self):
        adapter = CodexStateAdapter(start=False)
        adapter._state["source"] = "native-local"
        with patch("native_codex.dispatch_desktop_action") as fallback:
            for action in ("fast", "pin", "new", "fork", "mic", "submit"):
                adapter.desktop_action(action, pressed=True)
                adapter.desktop_action(action, pressed=False)
            adapter.desktop_action("steer", pressed=True)
        self.assertEqual(
            [call.args[0] for call in fallback.call_args_list],
            ["fast", "pin", "new", "fork", "mic", "submit"],
        )

    def test_submit_uses_cmd_enter_when_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[desktop]\ncomposerEnterBehavior = "cmdAlways"\n'
            )
            self.assertEqual(composer_enter_behavior(path), "cmdAlways")

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

    def test_rollout_tail_seeds_active_lifecycle_before_recent_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rollout.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-07-26T02:22:14.267Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    },
                    separators=(",", ":"),
                )
                + "\n"
                + json.dumps(
                    {
                        "timestamp": "2026-07-26T02:22:15.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "content": "x" * 2048,
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
            tail = RolloutTail(path, recent_bytes=128)
            tail.update()
            self.assertGreater(tail.started_at, tail.completed_at)

    def test_task_complete_with_error_is_failure(self):
        tail = RolloutTail.streamed("/remote/rollout.jsonl")
        records = (
            json.dumps(
                {
                    "timestamp": "2026-07-25T05:02:48.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "timestamp": "2026-07-25T05:17:52.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "error": {"message": "request timed out"},
                    },
                }
            )
            + "\n"
        ).encode()
        self.assertTrue(tail.feed(records))
        self.assertGreaterEqual(tail.error_at, tail.completed_at)

        next_turn = (
            json.dumps(
                {
                    "timestamp": "2026-07-26T01:55:22.984Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n"
        ).encode()
        self.assertTrue(tail.feed(next_turn))
        self.assertEqual(tail.error_at, 0)
        self.assertGreater(tail.started_at, tail.completed_at)

        interrupted = (
            json.dumps(
                {
                    "timestamp": "2026-07-26T01:55:34.181Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_aborted",
                        "reason": "interrupted",
                    },
                }
            )
            + "\n"
        ).encode()
        self.assertTrue(tail.feed(interrupted))
        self.assertEqual(tail.error_at, 0)
        self.assertGreaterEqual(tail.completed_at, tail.started_at)

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

    def test_restart_scan_does_not_promote_an_old_unwatched_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale_path = root / (
                "rollout-2026-07-01T00-00-00-"
                "00000000-0000-0000-0000-000000000005.jsonl"
            )
            stale_path.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": "00000000-0000-0000-0000-000000000005",
                    "thread_source": "user",
                },
            }) + "\n")
            stale_seconds = time.time() - 86_400
            os.utime(stale_path, (stale_seconds, stale_seconds))
            recent_path = root / "recent.jsonl"
            adapter = NativeCodex(start=False)
            adapter._local_threads = [{
                "id": "recent",
                "hostId": "local",
                "title": "Recent",
                "path": str(recent_path),
                "recencyAt": int(time.time() * 1000),
                "status": {"type": "notLoaded"},
            }]
            adapter._merge_threads()

            adapter._discover_rollouts(root)

            self.assertEqual(adapter._threads[0]["id"], "recent")
            stale = next(
                thread
                for thread in adapter._local_threads
                if thread["id"].endswith("0005")
            )
            self.assertLess(
                stale["recencyAt"],
                adapter._threads[0]["recencyAt"],
            )

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
                patch.object(
                    NativeCodex,
                    "_sync_remote_hosts",
                    return_value=False,
                ),
                patch(
                    "native_codex.GlobalStateWatcher",
                    lambda _path, callback: Watcher(callback),
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

    def test_local_pre_authorized_escalation_stays_thinking(self):
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
            self.assertEqual(
                adapter.snapshot()["slots"][0]["status"],
                "thinking",
            )

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

    def test_remote_rollout_approval_publishes_input(self):
        adapter = NativeCodex(start=False)
        adapter._threads = [
            {
                "id": "thread-remote",
                "name": "Remote approval",
                "path": "/remote/rollout.jsonl",
                "hostId": "remote-ssh-discovered:lite-shanghai",
                "status": {"type": "notLoaded"},
            }
        ]
        tail = RolloutTail.streamed("/remote/rollout.jsonl")
        tail.started_at = 100
        tail.pending_input.add("call-1")
        adapter._tails[
            "remote-ssh-discovered:lite-shanghai:/remote/rollout.jsonl"
        ] = tail
        adapter._publish()
        self.assertEqual(adapter.snapshot()["slots"][0]["status"], "input")

    def test_explicit_user_input_is_input_even_for_local_task(self):
        adapter = NativeCodex(start=False)
        adapter._threads = [
            {
                "id": "thread-local",
                "name": "Question",
                "path": "/local/rollout.jsonl",
                "hostId": "local",
                "status": {"type": "notLoaded"},
            }
        ]
        tail = RolloutTail.streamed("/local/rollout.jsonl")
        tail.started_at = 100
        tail.pending_input.add("call-1")
        tail.pending_explicit_input.add("call-1")
        adapter._tails["/local/rollout.jsonl"] = tail
        adapter._publish()
        self.assertEqual(adapter.snapshot()["slots"][0]["status"], "input")

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

    def test_usage_converts_current_used_percent_to_remaining_without_prediction(self):
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

    def test_usage_rejects_missing_or_unsupported_window_durations(self):
        result = usage_windows(
            {
                "rateLimits": {
                    "primary": {"usedPercent": 20},
                    "secondary": {
                        "usedPercent": 90,
                        "windowDurationMins": 1440,
                    },
                }
            }
        )
        self.assertEqual(result, [])

    def test_usage_rejects_percentages_outside_the_valid_range(self):
        result = usage_windows(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": -1,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": 101,
                        "windowDurationMins": 10080,
                    },
                }
            }
        )
        self.assertEqual(result, [])

    def test_usage_rejects_boolean_percentages(self):
        result = usage_windows(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": True,
                        "windowDurationMins": 300,
                    },
                    "secondary": {
                        "usedPercent": False,
                        "windowDurationMins": 10080,
                    },
                }
            }
        )
        self.assertEqual(result, [])

    def test_reads_codex_managed_remote_ssh_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "global.json"
            path.write_text(
                json.dumps(
                    {
                        "codex-managed-remote-connections": [
                            {
                                "hostId": "remote-ssh-discovered:lite-shanghai",
                                "alias": "lite-shanghai",
                            },
                            {"hostId": "local", "alias": "ignored"},
                        ]
                    }
                )
            )
            self.assertEqual(
                remote_hosts_from_state(path),
                {
                    "remote-ssh-discovered:lite-shanghai":
                    "lite-shanghai"
                },
            )

    def test_remote_rollout_merges_and_promotes_across_hosts(self):
        adapter = NativeCodex(start=False)
        adapter._local_threads = [
            {
                "id": "local-1",
                "hostId": "local",
                "name": "Local",
                "path": "/tmp/local-rollout",
                "recencyAt": 1000,
                "status": {"type": "notLoaded"},
            }
        ]
        host_id = "remote-ssh-discovered:lite-shanghai"
        self.assertTrue(
            adapter._handle_remote(
                host_id,
                {
                    "type": "inventory",
                    "threads": [
                        {
                            "id": "remote-1",
                            "name": "Remote",
                            "path": "/home/admin/rollout.jsonl",
                            "recencyAt": 500,
                            "status": {"type": "notLoaded"},
                        }
                    ],
                },
            )
        )
        self.assertEqual(
            [thread["id"] for thread in adapter._threads],
            ["local-1", "remote-1"],
        )
        initial = (
            json.dumps(
                {
                    "timestamp": "2026-07-25T00:00:00.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n"
        ).encode()
        self.assertTrue(
            adapter._handle_remote(
                host_id,
                {
                    "type": "rollout",
                    "path": "/home/admin/rollout.jsonl",
                    "reset": True,
                    "data": base64.b64encode(initial).decode(),
                },
            )
        )
        self.assertEqual(adapter._threads[0]["id"], "local-1")
        started = (
            json.dumps(
                {
                    "timestamp": "2026-07-25T00:00:01.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n"
        ).encode()
        self.assertTrue(adapter._handle_remote(
            host_id,
            {
                "type": "rollout",
                "path": "/home/admin/rollout.jsonl",
                "data": base64.b64encode(started).decode(),
            },
        ))
        self.assertEqual(adapter._threads[0]["id"], "remote-1")
        adapter._publish()
        slot = adapter.snapshot()["slots"][0]
        self.assertEqual(slot["hostId"], host_id)
        self.assertTrue(slot["hostOnline"])
        self.assertEqual(slot["status"], "thinking")
        self.assertTrue(adapter._handle_remote(host_id, {"type": "offline"}))
        adapter._publish()
        slot = adapter.snapshot()["slots"][0]
        self.assertFalse(slot["hostOnline"])
        self.assertEqual(slot["status"], "thinking")

    def test_remote_sqlite_warning_preserves_inventory(self):
        adapter = NativeCodex(start=False, enable_remote=True)
        host_id = "remote-ssh-discovered:legacy"
        adapter._remote_online[host_id] = True
        adapter._remote_threads[host_id] = [{
            "id": "remote-existing",
            "hostId": host_id,
            "path": "/home/admin/rollout.jsonl",
            "recencyAt": 100,
        }]
        adapter._merge_threads()
        with patch("native_codex.sys.stderr"):
            self.assertTrue(adapter._handle_remote(host_id, {
                "type": "warning",
                "message": "malformed database schema",
                "sqliteVersion": "3.7.17",
                "mode": "rollout-only",
            }))
        self.assertEqual(adapter._threads[0]["id"], "remote-existing")
        self.assertEqual(
            adapter._remote_errors[host_id],
            "malformed database schema",
        )

    def test_five_newer_local_tasks_evict_an_older_ssh_task_globally(self):
        adapter = NativeCodex(start=False)
        adapter._local_threads = [
            {
                "id": f"local-{index}",
                "hostId": "local",
                "recencyAt": 2000 + index,
            }
            for index in range(5)
        ]
        host_id = "remote-ssh-discovered:lite-shanghai"
        adapter._remote_threads[host_id] = [{
            "id": "remote-old",
            "hostId": host_id,
            "recencyAt": 1000,
        }]

        adapter._merge_threads()

        self.assertEqual(len(adapter._threads), 5)
        self.assertNotIn(
            "remote-old",
            [thread["id"] for thread in adapter._threads],
        )
        self.assertEqual(
            [thread["id"] for thread in adapter._threads],
            ["local-4", "local-3", "local-2", "local-1", "local-0"],
        )

    def test_remote_session_meta_enters_recent_before_inventory(self):
        adapter = NativeCodex(start=False)
        host_id = "remote-ssh-discovered:lite-shanghai"
        thread_id = "00000000-0000-0000-0000-000000000099"
        path = (
            "/home/admin/.codex/sessions/2026/07/25/"
            f"rollout-2026-07-25T00-00-01-{thread_id}.jsonl"
        )
        chunk = "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": thread_id,
                            "thread_source": "user",
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-25T00:00:01.000Z",
                        "type": "event_msg",
                        "payload": {"type": "task_started"},
                    }
                ),
            ]
        ).encode()
        self.assertTrue(
            adapter._handle_remote(
                host_id,
                {
                    "type": "rollout",
                    "path": path,
                    "reset": True,
                    "data": base64.b64encode(chunk + b"\n").decode(),
                },
            )
        )
        self.assertEqual(adapter._threads[0]["id"], thread_id)
        self.assertTrue(adapter._threads[0]["_provisional"])

    def test_open_slot_prefers_bridge_for_remote_task(self):
        adapter = NativeCodex(start=False)
        host_id = "remote-ssh-discovered:lite-shanghai"
        adapter._state["slots"] = [
            {
                "threadKey": "remote-1",
                "title": "Remote task",
                "hostId": host_id,
            }
        ]
        with patch(
            "native_codex.dispatch_bridge_thread",
            return_value=True,
        ) as bridge, patch(
            "native_codex.open_remote_thread_from_dock"
        ) as dock, patch("native_codex.subprocess.Popen") as deep_link:
            adapter.open_slot(0)
        bridge.assert_called_once_with("remote-1", 0)
        dock.assert_not_called()
        deep_link.assert_not_called()

    def test_remote_task_uses_dock_recent_when_bridge_is_unavailable(self):
        adapter = NativeCodex(start=False)
        host_id = "remote-ssh-discovered:lite-shanghai"
        adapter._remote_threads[host_id] = [
            {
                "id": "remote-1",
                "hostId": host_id,
                "title": "Remote task",
                "path": "/home/admin/rollout.jsonl",
            }
        ]
        adapter._state["slots"] = [{
            "threadKey": "remote-1",
            "title": "Remote task",
            "hostId": host_id,
        }]
        with patch(
            "native_codex.dispatch_bridge_thread",
            return_value=False,
        ), patch(
            "native_codex.open_remote_thread_from_dock"
        ) as dock, patch(
            "native_codex.show_native_navigation_notice"
        ) as notice, patch("native_codex.subprocess.Popen") as deep_link:
            # This is the actual D200 action-thread call signature.
            adapter.open_thread("remote-1")
            adapter.open_thread("remote-1")
        self.assertEqual(dock.call_count, 2)
        dock.assert_called_with("Remote task")
        notice.assert_called_once()
        deep_link.assert_not_called()

    def test_local_task_uses_deep_link_when_bridge_is_unavailable(self):
        adapter = NativeCodex(start=False)
        with patch(
            "native_codex.dispatch_bridge_thread",
            return_value=False,
        ), patch("native_codex.subprocess.Popen") as popen, patch(
            "native_codex.show_native_navigation_notice"
        ) as notice:
            adapter.open_thread("local-1", host_id="local")
        self.assertEqual(
            popen.call_args.args[0],
            ["/usr/bin/open", "codex://threads/local-1"],
        )
        notice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
