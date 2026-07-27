import importlib.util
import io
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = Path(__file__).parents[1] / "standalone" / "d200.py"
SPEC = importlib.util.spec_from_file_location("d200", MODULE_PATH)
D200 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(D200)


class ProtocolTests(unittest.TestCase):
    def test_idle_button_read_uses_blocking_timeout(self):
        device = object.__new__(D200.D200)
        device.handle = Mock()
        device.handle.read.return_value = []
        self.assertIsNone(device.read_button())
        device.handle.read.assert_called_once_with(D200.REPORT_SIZE, 50)

    def test_default_theme_contains_every_runtime_surface(self):
        expected_tasks = {"idle", "thinking", "complete", "input", "error"}
        expected_surfaces = set(D200.ACTION_KEYS.values()) | {"usage"}
        theme_path = Path(__file__).parents[1] / "standalone" / "icon-theme.default.json"
        theme = json.loads(theme_path.read_text())
        self.assertEqual(set(theme["tasks"]), expected_tasks)
        self.assertEqual(set(theme["surfaces"]), expected_surfaces)
        for section, names in (
            ("tasks", expected_tasks),
            ("surfaces", expected_surfaces),
        ):
            for name in names:
                self.assertTrue(
                    (theme_path.parent / theme[section][name]).is_file(),
                    f"missing {section}.{name}",
                )

    def test_packet_header_and_endianness(self):
        result = D200.packet(0x000A, b"75")
        self.assertEqual(len(result), 1024)
        self.assertEqual(result[:4], b"||\x00\x0a")
        self.assertEqual(struct.unpack("<I", result[4:8])[0], 2)
        self.assertEqual(result[8:10], b"75")

    def test_keep_alive_payload_fits_one_report(self):
        payload = b"0|0|0|21:45:00|0"
        result = D200.packet(0x0006, payload)
        self.assertEqual(result[:4], b"||\x00\x06")
        self.assertEqual(result[8 : 8 + len(payload)], payload)

    def test_profile_contains_all_buttons(self):
        result = D200.make_profile({0: b"png-zero", 5: b"png-five"})
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len(manifest), 14)
            self.assertTrue(manifest["0_0"]["ViewParam"][0]["Icon"])
            self.assertTrue(manifest["0_1"]["ViewParam"][0]["Icon"])
            self.assertEqual(manifest["1_0"]["ViewParam"][0]["Icon"], "")

    def test_partial_profile_contains_only_changed_buttons(self):
        result = D200.make_valid_profile(
            {2: b"png-two", 6: b"png-six"},
            partial=True,
        )
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(set(manifest), {"2_0", "1_1"})

    def test_status_mapping(self):
        self.assertEqual(D200.normalize_status("working"), "thinking")
        self.assertEqual(D200.normalize_status("input"), "input")
        self.assertEqual(D200.normalize_status("requires-input"), "input")
        self.assertEqual(D200.normalize_status("done"), "complete")
        self.assertEqual(D200.normalize_status("error"), "error")
        self.assertEqual(D200.normalize_status("failed"), "error")

    def test_native_layout_uses_first_five_keys_and_second_row_actions(self):
        self.assertEqual(D200.ACTIVE_SLOTS, 5)
        self.assertEqual(D200.USAGE_DISPLAY_KEY, 6)
        self.assertEqual(
            D200.ACTION_KEYS,
            {
                5: "fast",
                7: "pin",
                8: "new",
                9: "fork",
                10: "steer",
                11: "mic",
                12: "submit",
            },
        )
        self.assertEqual(D200.FOCUS_KEY, 13)

    def test_icon_digest_ignores_selection_and_subpercent_usage_changes(self):
        state = {
            "connected": True,
            "selected": "thread-a",
            "slots": [
                {
                    "threadKey": "thread-a",
                    "title": "Build",
                    "status": "thinking",
                    "selected": True,
                }
            ],
            "usage": {
                "windows": [
                    {"kind": "weekly", "remainingPercent": 79.41},
                    {"kind": "five-hour", "remainingPercent": 92.2},
                ]
            },
        }
        changed = {
            **state,
            "selected": "thread-b",
            "slots": [{**state["slots"][0], "selected": False}],
            "usage": {
                "windows": [
                    {"kind": "weekly", "remainingPercent": 79.49},
                    {"kind": "five-hour", "remainingPercent": 92.3},
                ]
            },
        }
        self.assertEqual(D200.icon_digest(state), D200.icon_digest(changed))

    def test_icon_digest_changes_when_rendered_usage_changes(self):
        state = {
            "connected": True,
            "slots": [],
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 79.49}]
            },
        }
        changed = {
            **state,
            "usage": {
                "windows": [{"kind": "weekly", "remainingPercent": 80.51}]
            },
        }
        self.assertNotEqual(D200.icon_digest(state), D200.icon_digest(changed))

    def test_icon_digest_ignores_title_when_task_keys_do_not_render_text(self):
        state = {
            "connected": True,
            "slots": [
                {
                    "threadKey": "thread-a",
                    "title": "Temporary title",
                    "status": "thinking",
                }
            ],
            "usage": {"windows": []},
        }
        changed = {
            **state,
            "slots": [{**state["slots"][0], "title": "Final title"}],
        }
        self.assertEqual(D200.icon_digest(state), D200.icon_digest(changed))

    def test_profile_reports_reconstruct_payload(self):
        profile = bytes(range(256)) * 9
        reports = list(D200.D200.profile_reports(object(), profile))
        self.assertEqual(len(reports), 3)
        self.assertEqual(reports[0][:2], b"||")
        self.assertEqual(struct.unpack("<I", reports[0][4:8])[0], len(profile))
        reconstructed = reports[0][8:] + b"".join(reports[1:])
        self.assertEqual(reconstructed[: len(profile)], profile)

    def test_partial_profile_uses_dedicated_firmware_command(self):
        profile = b"partial-profile"
        reports = list(
            D200.D200.profile_reports(object(), profile, partial=True)
        )
        self.assertEqual(reports[0][:4], b"||\x00\x0d")
        self.assertEqual(
            struct.unpack("<I", reports[0][4:8])[0],
            len(profile),
        )

    def test_partial_update_does_not_build_full_cache_on_display_path(self):
        old_icons = {0: b"old-zero", 1: b"same-one"}
        old_digests = {
            index: D200.hashlib.sha256(icon).hexdigest()
            for index, icon in old_icons.items()
        }
        new_icons = {0: b"new-zero", 1: b"same-one"}
        calls = []

        def build(icons, partial=False):
            calls.append((icons, partial))
            return b"wire"

        with patch.object(D200, "make_valid_profile", side_effect=build):
            profile, _, partial = D200.build_display_update(
                new_icons,
                "applied-digest",
                old_digests,
            )
        self.assertEqual(profile, b"wire")
        self.assertTrue(partial)
        self.assertEqual(calls, [({0: b"new-zero"}, True)])

    def test_usb_hotplug_discovery_backs_off_after_fast_retry(self):
        missing = D200.D200NotFoundError("not connected")
        self.assertEqual(
            D200.reconnect_delay(missing, 1),
            0.5,
        )
        self.assertEqual(D200.reconnect_delay(missing, 2), 1.0)
        self.assertEqual(D200.reconnect_delay(missing, 100), 5.0)
        self.assertEqual(D200.reconnect_delay(OSError("write failed"), 100), 10.0)

    def test_cached_profile_replays_only_after_a_real_disconnect(self):
        self.assertFalse(D200.should_restore_cached_profile(False, b"profile"))
        self.assertFalse(D200.should_restore_cached_profile(True, None))
        self.assertTrue(D200.should_restore_cached_profile(True, b"profile"))

    def test_usb_reconnect_forgets_every_applied_key_digest(self):
        digest, keys = D200.display_baseline_after_connect(
            True,
            "previous-frame",
            {0: "key-zero", 11: "mic"},
        )
        self.assertEqual(digest, "")
        self.assertEqual(keys, {})

    def test_daemon_restart_keeps_the_applied_display_baseline(self):
        digest, keys = D200.display_baseline_after_connect(
            False,
            "previous-frame",
            {0: "key-zero"},
        )
        self.assertEqual(digest, "previous-frame")
        self.assertEqual(keys, {0: "key-zero"})

    def test_cached_button_mapping_survives_profile_version_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "profileVersion": D200.PROFILE_VERSION - 1,
                        "threadIds": ["thread-a", "thread-b"],
                    }
                )
            )
            with patch.object(D200, "CACHE_PATH", cache):
                self.assertEqual(
                    D200.load_cached_slots(),
                    ["thread-a", "thread-b"],
                )


if __name__ == "__main__":
    unittest.main()
