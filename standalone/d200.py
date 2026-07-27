#!/usr/bin/env python3
"""Direct Ulanzi D200 driver for openCodexMicro."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import hashlib
import io
import json
import os
import queue
import random
import signal
import struct
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

VID = 0x2207
PID = 0x0019
USAGE_PAGE = 0x000C
REPORT_SIZE = 1024
BUTTON_COUNT = 14
ACTIVE_SLOTS = 5
KEEP_ALIVE_SECONDS = 30.0
DEVICE_DISCOVERY_SECONDS = 0.5
DEVICE_DISCOVERY_MAX_SECONDS = 5.0
# Emergency switch for diagnosing firmware/HID output issues. Release
# installers explicitly enable display writes.
OUTPUT_WRITES_ENABLED = (
    os.environ.get(
        "OPEN_CODEX_MICRO_OUTPUT_WRITES",
        os.environ.get("CODEX_KEYBOARD_OUTPUT_WRITES"),
    )
    == "1"
)
# The D200 treats profile chunks as one time-sensitive file transaction. A
# long gap makes firmware abandon the transfer (observed as a 5 s timeout on
# packet 5), while macOS' synchronous SetReport already provides backpressure.
UPLOAD_PACKET_DELAY_SECONDS = 0.003
COMMAND_SETTLE_SECONDS = 0.050
PROFILE_VERSION = 26
CACHE_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "openCodexMicro"
    / "d200-profile-cache.json"
)
PROFILE_CACHE_PATH = CACHE_PATH.with_name("d200-last-profile.zip")
ACTION_KEYS = {
    5: "fast",
    7: "pin",
    8: "new",
    9: "fork",
    10: "steer",
    11: "mic",
    12: "submit",
}
USAGE_DISPLAY_KEY = 6
FOCUS_KEY = 13
DEFAULT_THEME_PATH = Path(__file__).with_name("icon-theme.default.json")
USER_THEME_PATH = Path(
    os.environ.get(
        "OPEN_CODEX_MICRO_ICON_THEME",
        os.environ.get(
            "CODEX_KEYBOARD_ICON_THEME",
            str(Path(__file__).with_name("icon-theme.json")),
        ),
    )
).expanduser()


def load_icon_theme() -> dict:
    """Merge the packaged theme with optional local overrides."""
    theme: dict = {"surfaces": {}, "tasks": {}, "usage": {}}
    for candidate in (DEFAULT_THEME_PATH, USER_THEME_PATH):
        try:
            loaded = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(loaded, dict):
            continue
        for section in ("surfaces", "tasks"):
            configured_assets = loaded.get(section)
            if not isinstance(configured_assets, dict):
                continue
            for name, configured in configured_assets.items():
                path = Path(str(configured)).expanduser()
                if not path.is_absolute():
                    path = candidate.parent / path
                theme[section][str(name)] = str(path)
        usage = loaded.get("usage")
        if isinstance(usage, dict):
            theme["usage"].update(usage)
    return theme


ICON_THEME = load_icon_theme()


class D200NotFoundError(OSError):
    """The daemon is healthy, but no matching USB device is currently present."""


def reconnect_delay(error: OSError, attempt: int) -> float:
    """Keep first hot-plug retries fast without enumerating USB forever."""
    if isinstance(error, D200NotFoundError):
        return min(
            DEVICE_DISCOVERY_MAX_SECONDS,
            DEVICE_DISCOVERY_SECONDS
            * (2 ** min(max(0, attempt - 1), 4)),
        )
    return min(10.0, 0.5 * max(1, attempt))


def should_restore_cached_profile(
    profile_restore_required: bool,
    cached_profile: bytes | None,
) -> bool:
    return profile_restore_required and cached_profile is not None


def display_baseline_after_connect(
    profile_restore_required: bool,
    runtime_digest: str,
    cached_key_digests: dict[int, str],
) -> tuple[str, dict[int, str]]:
    """Forget every applied key after a USB loss, but not a daemon restart."""
    if profile_restore_required:
        return "", {}
    return runtime_digest, cached_key_digests.copy()


def normalize_status(value: object) -> str:
    status = str(value or "").lower()
    if status in {"off", "empty"}:
        return "empty"
    if status in {"working", "thinking", "running", "in_progress"}:
        return "thinking"
    if status in {"unread", "complete", "completed", "done"}:
        return "complete"
    if status in {
        "input",
        "approval",
        "awaiting-approval",
        "awaiting-response",
        "requires-input",
        "waiting",
    }:
        return "input"
    if status in {"error", "failed"}:
        return "error"
    return "idle"


def load_latin_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = [
        "/System/Library/Fonts/SFCompactRounded.ttf",
        "/System/Library/Fonts/SFNSRounded.ttf",
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
        if bold
        else "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def text_width(draw, text: str, font) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def encode_png(
    image,
    colors: int = 24,
    method: str = "median",
) -> bytes:
    """Keep profiles small enough that visual refreshes do not monopolize HID."""
    from PIL import Image

    quantize_method = (
        Image.Quantize.MAXCOVERAGE
        if method == "coverage"
        else Image.Quantize.MEDIANCUT
    )
    indexed = image.convert("RGB").quantize(
        colors=colors,
        method=quantize_method,
        dither=Image.Dither.NONE,
    )
    output = io.BytesIO()
    bit_depth = 1 if colors <= 2 else 2 if colors <= 4 else 4 if colors <= 16 else 8
    indexed.save(output, "PNG", optimize=True, bits=bit_depth)
    return output.getvalue()


@lru_cache(maxsize=None)
def load_generated_surface(section: str, name: str):
    from PIL import Image

    configured = (ICON_THEME.get(section) or {}).get(name)
    if not configured:
        raise KeyError(f"Theme is missing {section}.{name}")
    path = Path(str(configured)).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"Theme asset not found: {path}")
    surface = Image.open(path).convert("RGBA")
    if surface.size != (196, 196):
        surface = surface.resize((196, 196), Image.Resampling.LANCZOS)
    plate = Image.new("RGBA", (196, 196), "#b8bebfff")
    plate.alpha_composite(surface)
    return plate


def render_icon(slot: int, agent: dict | None, connected: bool) -> bytes:
    agent = agent or {}
    status = (
        normalize_status(agent.get("status") or ("idle" if agent.get("threadKey") else "off"))
        if connected
        else "offline"
    )
    if status in {"empty", "offline"}:
        status = "idle"
    return encode_png(
        load_generated_surface("tasks", status),
        colors=256 if status == "complete" else 128,
        method="coverage" if status == "complete" else "median",
    )


@lru_cache(maxsize=128)
def render_agent_icon(
    slot: int,
    title: str | None,
    status: str | None,
    thread_key: str | None,
    connected: bool,
) -> bytes:
    return render_icon(
        slot,
        {"title": title, "status": status, "threadKey": thread_key},
        connected,
    )


@lru_cache(maxsize=None)
def render_action_icon(action: str) -> bytes:
    return encode_png(
        load_generated_surface("surfaces", action),
        colors=96,
        method="coverage",
    )


def render_usage_icon(usage: dict | None) -> bytes:
    windows = {item.get("kind"): item for item in (usage or {}).get("windows", [])}
    five_hour = windows.get("five-hour", {}).get("remainingPercent")
    weekly = windows.get("weekly", {}).get("remainingPercent")
    five_percent = None if five_hour is None else max(0, min(100, round(float(five_hour))))
    weekly_percent = None if weekly is None else max(0, min(100, round(float(weekly))))
    return render_usage_values(five_percent, weekly_percent)


@lru_cache(maxsize=32)
def render_usage_values(five_percent: int | None, weekly_percent: int | None) -> bytes:
    remaining = weekly_percent if weekly_percent is not None else five_percent
    usage_theme = ICON_THEME.get("usage") or {}
    if remaining is None:
        progress = str(usage_theme.get("unknown") or "#858c8f")
        number_text = "—"
    elif remaining >= 50:
        progress = str(usage_theme.get("high") or "#2fbd7f")
        number_text = str(remaining)
    elif remaining >= 20:
        progress = str(usage_theme.get("medium") or "#e89b2d")
        number_text = str(remaining)
    else:
        progress = str(usage_theme.get("low") or "#e45861")
        number_text = str(remaining)
    from PIL import Image, ImageColor, ImageDraw, ImageFilter

    # Supersampling preserves the shallow bevel and resin-tube highlights after
    # the image is reduced to the D200's 196 px, 96-color output.
    scale = 4
    image = load_generated_surface("surfaces", "usage").copy().resize(
        (196 * scale, 196 * scale),
        Image.Resampling.LANCZOS,
    )
    ring_offset_y = 5
    ring_box = (41, 32, 155, 146)
    tube_width = max(6, min(12, int(usage_theme.get("strokeWidth") or 10)))

    def scaled_box(box: tuple[float, float, float, float]) -> tuple[int, ...]:
        return tuple(
            round(value * scale)
            for value in (box[0], box[1] + ring_offset_y, box[2], box[3] + ring_offset_y)
        )

    def draw_arc(
        layer,
        box: tuple[float, float, float, float],
        extent: float,
        color,
        width: float,
    ) -> None:
        ImageDraw.Draw(layer).arc(
            scaled_box(box),
            -90,
            -90 + extent,
            fill=color,
            width=round(width * scale),
        )

    def rgb_variant(color: str, factor: float, toward_white: bool = False):
        values = ImageColor.getrgb(color)
        if toward_white:
            return tuple(round(value + (255 - value) * factor) for value in values)
        return tuple(round(value * factor) for value in values)

    extent = 0 if remaining is None else 360 * remaining / 100
    if remaining is not None:
        glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw_arc(
            glow,
            ring_box,
            extent,
            (*ImageColor.getrgb(progress), 150),
            tube_width + 8,
        )
        glow = glow.filter(ImageFilter.GaussianBlur(4.5 * scale))
        image.alpha_composite(glow)

    gauge = Image.new("RGBA", image.size, (0, 0, 0, 0))
    # A recessed physical channel, then a thick translucent resin tube.
    draw_arc(gauge, (39.5, 30.5, 156.5, 147.5), 360, (255, 255, 255, 145), 2.4)
    draw_arc(gauge, (41, 34, 155, 148), 360, (94, 108, 104, 145), tube_width + 6)
    draw_arc(gauge, ring_box, 360, (160, 172, 169, 255), tube_width + 2)
    draw_arc(gauge, ring_box, 360, (205, 214, 211, 255), 6)
    if remaining is not None:
        draw_arc(
            gauge,
            ring_box,
            extent,
            rgb_variant(progress, 0.54),
            tube_width + 4,
        )
        draw_arc(gauge, ring_box, extent, progress, tube_width)
        draw_arc(
            gauge,
            (41, 30.8, 155, 144.8),
            extent,
            (*rgb_variant(progress, 0.68, toward_white=True), 225),
            3.1,
        )
    image.alpha_composite(gauge)

    draw = ImageDraw.Draw(image)
    font_size = max(20, min(38, int(usage_theme.get("fontSize") or 36)))
    value_font = load_latin_font(font_size * scale, True)
    text_fill = str(usage_theme.get("text") or "#303638")
    if remaining is None:
        draw.text(
            (98 * scale, (89 + ring_offset_y) * scale),
            number_text,
            font=value_font,
            fill=text_fill,
            anchor="mm",
        )
    else:
        percent_size = max(
            12,
            min(24, int(usage_theme.get("percentFontSize") or 18)),
        )
        percent_font = load_latin_font(percent_size * scale, True)
        number_width = text_width(draw, number_text, value_font)
        percent_width = text_width(draw, "%", percent_font)
        start_x = 98 * scale - (number_width + percent_width + 2 * scale) / 2
        draw.text(
            (start_x, (89 + ring_offset_y) * scale),
            number_text,
            font=value_font,
            fill=text_fill,
            anchor="lm",
        )
        draw.text(
            (
                start_x + number_width + 2 * scale,
                (94 + ring_offset_y) * scale,
            ),
            "%",
            font=percent_font,
            fill=text_fill,
            anchor="lm",
        )
    image = image.resize((196, 196), Image.Resampling.LANCZOS)
    return encode_png(image, colors=96, method="coverage")


def make_profile(
    icons: dict[int, bytes],
    attempt: int = 0,
    partial: bool = False,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dummy.txt", bytes(random.getrandbits(8) for _ in range(1024 * attempt)))
        manifest = {}
        indexes = sorted(icons) if partial else range(BUTTON_COUNT)
        for index in indexes:
            key = f"{index % 5}_{index // 5}"
            icon_path = ""
            if index in icons:
                icon_path = f"Images/{index}_{uuid.uuid4()}.png"
                archive.writestr(icon_path, icons[index])
            manifest[key] = {"State": 0, "ViewParam": [{"Text": "", "Icon": icon_path}]}
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        archive.writestr("sentinel.txt", b"")
    return output.getvalue()


def make_valid_profile(
    icons: dict[int, bytes],
    partial: bool = False,
) -> bytes:
    # Some D200 firmware revisions reject 0x00/0x7c at these packet boundaries.
    for attempt in range(1000):
        profile = make_profile(icons, attempt, partial=partial)
        if all(profile[offset] not in (0x00, 0x7C) for offset in range(92152, len(profile), 1024)):
            return profile
    raise RuntimeError("Could not build a D200-compatible profile archive")


def build_display_update(
    icons: dict[int, bytes],
    base_digest: str,
    applied_key_digests: dict[int, str],
) -> tuple[bytes, dict[int, str], bool]:
    """Build only the wire payload needed for the next visible state."""
    key_digests = {
        index: hashlib.sha256(icon).hexdigest()
        for index, icon in icons.items()
    }
    changed_icons = {
        index: icon
        for index, icon in icons.items()
        if applied_key_digests.get(index) != key_digests[index]
    }
    if not changed_icons and base_digest:
        return b"", key_digests, False
    partial = (
        bool(base_digest)
        and len(applied_key_digests) == len(icons)
    )
    return (
        make_valid_profile(
            changed_icons if partial else icons,
            partial=partial,
        ),
        key_digests,
        partial,
    )


def make_recovery_profile() -> bytes:
    """Build a local fallback used only when a newly inserted D200 is blank.

    It deliberately does not call Codex: insertion recovery must work when the
    adapter is offline.  A later explicit profile refresh replaces it with live
    task state.
    """
    icons = {
        **{
            index: render_agent_icon(index, f"Agent {index + 1}", "offline", None, False)
            for index in range(ACTIVE_SLOTS)
        },
        **{index: render_action_icon(action) for index, action in ACTION_KEYS.items()},
        USAGE_DISPLAY_KEY: render_usage_values(None, None),
    }
    return make_valid_profile(icons)


def packet(command: int, payload: bytes, total_length: int | None = None) -> bytes:
    if len(payload) > REPORT_SIZE - 8:
        raise ValueError("Command payload exceeds one D200 report")
    header = b"||" + struct.pack(">H", command) + struct.pack("<I", total_length or len(payload))
    return (header + payload).ljust(REPORT_SIZE, b"\0")


class D200:
    def __init__(self):
        try:
            import hid
        except ImportError as error:
            raise RuntimeError(
                "Missing hidapi; run: python3 -m pip install -r standalone/requirements.txt"
            ) from error
        matches = [
            entry
            for entry in hid.enumerate(VID, PID)
            if entry.get("usage_page") in (None, 0, USAGE_PAGE)
        ]
        if not matches:
            raise D200NotFoundError("Ulanzi D200 not found")
        preferred = next((entry for entry in matches if entry.get("usage_page") == USAGE_PAGE), matches[0])
        self.info = {
            key: value.decode(errors="replace") if isinstance(value, bytes) else value
            for key, value in preferred.items()
            if key in {"path", "manufacturer_string", "product_string", "serial_number", "usage_page", "interface_number"}
        }
        self.handle = hid.device()
        try:
            self.handle.open_path(preferred["path"])
        except OSError as error:
            raise OSError(
                "D200 is present but its HID interface cannot be opened"
            ) from error
        # Timed reads provide the same bounded input latency without spinning
        # through hidapi when there is no report available.
        self.handle.set_nonblocking(False)

    def write(self, report: bytes, context: str = "command") -> None:
        # hidapi reserves the first byte for the report ID; D200 uses report ID 0.
        for attempt in range(6):
            started = time.monotonic()
            written = self.handle.write(b"\0" + report)
            elapsed = time.monotonic() - started
            if elapsed >= 0.250:
                print(
                    f"D200 slow HID write: {elapsed * 1000:.0f}ms "
                    f"for {context} (attempt {attempt + 1}).",
                    file=sys.stderr,
                    flush=True,
                )
            if written > 0:
                return
            time.sleep(0.01 * (attempt + 1))
        raise OSError("D200 HID write failed after 6 retries")

    def command(self, command: int, payload: bytes) -> None:
        self.write(packet(command, payload), f"command 0x{command:04x}")

    def keep_alive(self) -> None:
        # Mode 1 keeps the session alive and lets firmware own the wide clock.
        current_time = time.strftime("%H:%M:%S")
        self.command(0x0006, f"1|0|0|{current_time}|0".encode())

    def set_brightness(self, percent: int = 100) -> None:
        self.command(0x000A, str(max(0, min(100, percent))).encode())

    def upload(self, profile: bytes) -> None:
        for report in self.profile_reports(profile):
            self.write(report, "profile upload")
            time.sleep(UPLOAD_PACKET_DELAY_SECONDS)

    def profile_reports(self, profile: bytes, partial: bool = False):
        command = 0x000D if partial else 0x0001
        yield packet(command, profile[:1016], len(profile))
        for offset in range(1016, len(profile), REPORT_SIZE):
            yield profile[offset : offset + REPORT_SIZE].ljust(REPORT_SIZE, b"\0")

    def read_button(self, timeout_ms: int = 50) -> tuple[int, bool] | None:
        raw = bytes(self.handle.read(REPORT_SIZE, timeout_ms))
        if raw[:1] == b"\0" and raw[1:3] == b"||":
            raw = raw[1:]
        if len(raw) < 12 or raw[:2] != b"||":
            return None
        command = struct.unpack(">H", raw[2:4])[0]
        if command not in (0x0101, 0x0102):
            return None
        index = raw[9]
        return (index, raw[11] == 1) if index < BUTTON_COUNT else None

    def close(self) -> None:
        self.handle.close()


def bare_metal_self_test() -> dict:
    """Exercise profile construction without opening USB or Codex.

    This is intentionally the first debugging step: it proves the archive,
    image encoder, report framing and firmware-clock payload before touching a
    real D200.
    """
    icons = {
        **{index: render_agent_icon(index, f"Agent {index + 1}", "thinking", f"local:{index}", True)
           for index in range(ACTIVE_SLOTS)},
        **{index: render_action_icon(action) for index, action in ACTION_KEYS.items()},
        USAGE_DISPLAY_KEY: render_usage_values(80, 70),
    }
    profile = make_valid_profile(icons)
    recovery_profile = make_recovery_profile()
    reports = list(D200.profile_reports(object(), profile))
    with zipfile.ZipFile(io.BytesIO(profile)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert len(manifest) == BUTTON_COUNT
    assert manifest["3_2"]["ViewParam"][0]["Icon"] == "", "firmware clock must own key 13"
    with zipfile.ZipFile(io.BytesIO(recovery_profile)) as archive:
        recovery_manifest = json.loads(archive.read("manifest.json"))
    assert len(recovery_manifest) == BUTTON_COUNT
    clock_payload = b"1|0|0|12:34:56|0"
    assert packet(0x0006, clock_payload)[8 : 8 + len(clock_payload)] == clock_payload
    return {
        "ok": True,
        "profileBytes": len(profile),
        "reports": len(reports),
        "recoveryProfileBytes": len(recovery_profile),
        "icons": len(icons),
        "clockKey": FOCUS_KEY,
    }


def diagnose_device() -> dict:
    """Open the D200 once and report its actual HID endpoint without writing."""
    device = D200()
    try:
        return {
            "ok": True,
            "vid": f"0x{VID:04x}",
            "pid": f"0x{PID:04x}",
            "usagePage": f"0x{USAGE_PAGE:04x}",
            "reportSize": REPORT_SIZE,
            "device": device.info,
            "writesPerformed": 0,
        }
    finally:
        device.close()


def icon_digest(state: dict) -> str:
    def normalized_percent(value: object) -> int | None:
        if value is None:
            return None
        try:
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return None

    usage_windows = {
        item.get("kind"): normalized_percent(item.get("remainingPercent"))
        for item in (state.get("usage") or {}).get("windows", [])
    }
    displayed_usage = (
        usage_windows.get("weekly")
        if usage_windows.get("weekly") is not None
        else usage_windows.get("five-hour")
    )
    stable = {
        "profileVersion": PROFILE_VERSION,
        "connected": state.get("connected"),
        "slots": [
            {
                "threadKey": item.get("threadKey"),
                "status": item.get("status"),
            }
            for item in state.get("slots", [])[:ACTIVE_SLOTS]
        ],
        "usage": displayed_usage,
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def load_cached_digest() -> str:
    try:
        cached = json.loads(CACHE_PATH.read_text())
        if cached.get("profileVersion") == PROFILE_VERSION:
            return str(cached.get("digest") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return ""


def load_cached_slots() -> list[str]:
    try:
        cached = json.loads(CACHE_PATH.read_text())
        # The physical screen keeps displaying the previous profile while a
        # new version is prepared. Keep that exact button mapping across
        # upgrades and replace it atomically only after upload succeeds.
        return [
            str(value)
            for value in cached.get("threadIds", [])
            if value
        ][:ACTIVE_SLOTS]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return []


def load_cached_key_digests() -> dict[int, str]:
    try:
        cached = json.loads(CACHE_PATH.read_text())
        if cached.get("profileVersion") != PROFILE_VERSION:
            return {}
        return {
            int(index): str(digest)
            for index, digest in (cached.get("keyDigests") or {}).items()
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def save_cached_digest(
    digest: str,
    thread_ids: list[str] | None = None,
    key_digests: dict[int, str] | None = None,
) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = CACHE_PATH.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "profileVersion": PROFILE_VERSION,
                    "digest": digest,
                    "threadIds": list(thread_ids or []),
                    "keyDigests": {
                        str(index): value
                        for index, value in (key_digests or {}).items()
                    },
                    "updatedAt": time.time(),
                },
                separators=(",", ":"),
            )
        )
        temporary.replace(CACHE_PATH)
    except OSError as error:
        print(f"D200 profile cache write failed: {error}", file=sys.stderr, flush=True)


def load_cached_profile() -> bytes | None:
    """Return the last fully transferred archive, never a partial write."""
    try:
        profile = PROFILE_CACHE_PATH.read_bytes()
        with zipfile.ZipFile(io.BytesIO(profile)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        if len(manifest) != BUTTON_COUNT:
            return None
        return profile
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError, TypeError):
        return None


def save_cached_profile(profile: bytes) -> None:
    try:
        PROFILE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = PROFILE_CACHE_PATH.with_suffix(".tmp")
        temporary.write_bytes(profile)
        temporary.replace(PROFILE_CACHE_PATH)
    except OSError as error:
        print(f"D200 profile archive cache write failed: {error}", file=sys.stderr, flush=True)


def run(
    once: bool = False,
    output_writes: bool = OUTPUT_WRITES_ENABLED,
    force_profile: bool = False,
) -> None:
    stopped = False

    def stop(*_args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    reconnect_attempt = 0
    profile_restore_required = False
    last_connection_error = ""
    last_connection_log_at = 0.0
    runtime_applied_digest = "" if force_profile else load_cached_digest()
    from native_codex import NativeCodex

    native_adapter = NativeCodex()
    while not stopped:
        device = None
        session_stop = threading.Event()
        state_thread = None
        action_thread = None
        render_thread = None
        cache_thread = None
        agent_executor = None
        retry_delay = 0.0
        try:
            device = D200()
            cached_profile = load_cached_profile()
            # A normal daemon restart leaves the D200's last screen intact.
            # A reconnect after an actual USB loss does not, so replay the last
            # known-good archive before accepting any buttons.
            restore_after_reconnect = should_restore_cached_profile(
                profile_restore_required,
                cached_profile,
            )
            # A disk digest cannot prove that the D200 still contains a complete
            # profile after a USB disconnect, interrupted transfer, or power
            # loss. Force the first live frame after reconnect to contain every
            # key, even when the cached archive itself is unavailable. A normal
            # daemon restart with no observed disconnect may retain the digest.
            baseline_digest, applied_key_digests = display_baseline_after_connect(
                profile_restore_required,
                runtime_applied_digest,
                load_cached_key_digests(),
            )
            applied_digest = [baseline_digest]
            # The D200 may still show the previous version while the new
            # profile is rendered and transferred. Its cached mapping remains
            # authoritative until the replacement upload commits.
            active_thread_routes = [
                {"threadKey": thread_id, "hostId": None, "title": ""}
                for thread_id in load_cached_slots()
            ]
            last_keep_alive = time.monotonic()
            stable_since = time.monotonic()
            state_ready = threading.Event()
            last_button_at = time.monotonic()
            action_events: queue.Queue[
                tuple[int, bool, float, dict | None]
            ] = queue.Queue()
            agent_executor = ThreadPoolExecutor(
                max_workers=3,
                thread_name_prefix="d200-agent",
            )
            prepared_profiles: queue.Queue[
                tuple[
                    str,
                    str,
                    bytes,
                    list[dict],
                    dict[int, str],
                    bool,
                    float,
                    dict[int, bytes],
                ]
            ] = queue.Queue(maxsize=1)
            cache_profiles: queue.Queue[
                tuple[str, dict[int, bytes]]
            ] = queue.Queue(maxsize=1)
            refresh_condition = threading.Condition()
            target = [
                {
                    "digest": applied_digest[0],
                    "state": None,
                    "changedAt": time.monotonic(),
                }
            ]
            rendered_signature = [
                (applied_digest[0], applied_digest[0])
            ]
            print("D200 connected; native Codex mode active.", flush=True)

            def replace_prepared(
                value: tuple[
                    str,
                    str,
                    bytes,
                    list[dict],
                    dict[int, str],
                    bool,
                    float,
                    dict[int, bytes],
                ] | None
            ) -> None:
                while True:
                    try:
                        prepared_profiles.get_nowait()
                    except queue.Empty:
                        break
                if value is not None:
                    prepared_profiles.put_nowait(value)

            def poll_state() -> None:
                # Revision zero is the adapter's private "starting" placeholder,
                # not a displayable framebuffer. Waiting for revision one avoids
                # a full offline upload immediately followed by the real frame.
                native_revision = 0
                while not stopped and not session_stop.is_set():
                    revision, state = native_adapter.wait_for_change(
                        native_revision,
                        timeout=0.5,
                    )
                    if stopped or session_stop.is_set():
                        return
                    if revision == native_revision:
                        continue
                    native_revision = revision
                    now = time.monotonic()
                    digest = icon_digest(state)
                    with refresh_condition:
                        if digest != target[0]["digest"]:
                            target[0] = {
                                "digest": digest,
                                "state": state,
                                "changedAt": now,
                            }
                            replace_prepared(None)
                            refresh_condition.notify_all()
                            print(
                                "D200 state change detected at "
                                f"{time.strftime('%H:%M:%S')}; "
                                "render queued.",
                                flush=True,
                            )
                    state_ready.set()

            def render_profiles() -> None:
                fixed_icons = {
                    index: render_action_icon(action)
                    for index, action in ACTION_KEYS.items()
                }
                while not stopped and not session_stop.is_set():
                    with refresh_condition:
                        ready = refresh_condition.wait_for(
                            lambda: (
                                stopped
                                or session_stop.is_set()
                                or (
                                    applied_digest[0],
                                    target[0]["digest"],
                                )
                                != rendered_signature[0]
                            ),
                            timeout=0.5,
                        )
                        if not ready:
                            continue
                        if stopped or session_stop.is_set():
                            return
                        request = target[0].copy()
                        if request["digest"] == applied_digest[0]:
                            rendered_signature[0] = (
                                applied_digest[0],
                                request["digest"],
                            )
                            replace_prepared(None)
                            continue

                    state = request["state"] or {
                        "connected": False,
                        "slots": [],
                        "usage": None,
                    }
                    slots = state.get("slots", [])
                    icons = fixed_icons.copy()
                    for index in range(ACTIVE_SLOTS):
                        slot = slots[index] if index < len(slots) else {}
                        icons[index] = render_agent_icon(
                            index,
                            slot.get("title"),
                            slot.get("status"),
                            slot.get("threadKey"),
                            bool(state.get("connected")),
                        )
                    icons[USAGE_DISPLAY_KEY] = render_usage_icon(state.get("usage"))
                    base_digest = applied_digest[0]
                    thread_routes = [
                        {
                            "threadKey": str(slot.get("threadKey") or ""),
                            "hostId": str(slot.get("hostId") or "local"),
                            "title": str(slot.get("title") or ""),
                        }
                        for slot in slots[:ACTIVE_SLOTS]
                    ]
                    try:
                        profile, key_digests, partial = (
                            build_display_update(
                                icons,
                                base_digest,
                                applied_key_digests,
                            )
                        )
                    except RuntimeError as error:
                        print(
                            f"D200 profile render failed: {error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        session_stop.wait(1)
                        continue

                    with refresh_condition:
                        if request["digest"] != target[0]["digest"]:
                            continue
                        replace_prepared(
                            (
                                base_digest,
                                request["digest"],
                                profile,
                                thread_routes,
                                key_digests,
                                partial,
                                request["changedAt"],
                                icons,
                            )
                        )
                        rendered_signature[0] = (
                            base_digest,
                            request["digest"],
                        )
                        print(
                            "D200 profile rendered "
                            f"{(time.monotonic() - request['changedAt']) * 1000:.0f}ms "
                            "after state detection.",
                            flush=True,
                        )

            def build_profile_cache() -> None:
                while not stopped and not session_stop.is_set():
                    try:
                        digest, icons = cache_profiles.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    # Cache maintenance is never on the state-to-display path.
                    if session_stop.wait(0.250):
                        return
                    try:
                        profile = make_valid_profile(icons)
                    except RuntimeError as error:
                        print(
                            f"D200 background cache render failed: {error}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                    if digest == applied_digest[0]:
                        save_cached_profile(profile)
                        print(
                            "D200 full reconnect cache rebuilt in background.",
                            flush=True,
                        )

            def dispatch_actions() -> None:
                def dispatch_agent(
                    index: int,
                    route: dict | None,
                    captured_at: float,
                ) -> None:
                    try:
                        thread_id = (
                            str(route.get("threadKey") or "")
                            if route is not None
                            else ""
                        )
                        if not thread_id:
                            raise RuntimeError(
                                f"Native slot {index} has no displayed thread"
                            )
                        native_adapter.open_thread(
                            thread_id,
                            host_id=(
                                str(route.get("hostId"))
                                if route is not None
                                and route.get("hostId")
                                else None
                            ),
                            title=(
                                str(route.get("title") or "")
                                if route is not None
                                else ""
                            ),
                        )
                        elapsed = time.monotonic() - captured_at
                        if elapsed >= 0.100:
                            print(
                                f"D200 agent {index} down completed in "
                                f"{elapsed * 1000:.0f}ms.",
                                flush=True,
                            )
                    except (OSError, RuntimeError) as error:
                        print(
                            f"Codex agent dispatch failed: {error}",
                            file=sys.stderr,
                            flush=True,
                        )

                while not stopped and not session_stop.is_set():
                    try:
                        index, pressed, captured_at, thread_id = (
                            action_events.get(timeout=0.1)
                        )
                    except queue.Empty:
                        continue
                    try:
                        dispatched_at = time.monotonic()
                        phase = "down" if pressed else "up"
                        if pressed and index < ACTIVE_SLOTS:
                            agent_executor.submit(
                                dispatch_agent,
                                index,
                                thread_id,
                                captured_at,
                            )
                            continue
                        elif index in ACTION_KEYS:
                            action = ACTION_KEYS[index]
                            if action == "mic":
                                native_adapter.desktop_action(
                                    action,
                                    pressed=pressed,
                                )
                            elif pressed:
                                native_adapter.desktop_action(action)
                        elif pressed and index == USAGE_DISPLAY_KEY:
                            native_adapter.desktop_action("focus")
                        elif pressed and index == FOCUS_KEY:
                            native_adapter.desktop_action("focus")
                        elapsed = time.monotonic() - captured_at
                        if elapsed >= 0.100:
                            print(
                                f"D200 action {index} {phase} completed in "
                                f"{elapsed * 1000:.0f}ms "
                                f"(queue {max(0, dispatched_at - captured_at) * 1000:.0f}ms).",
                                flush=True,
                            )
                    except (OSError, RuntimeError) as error:
                        print(
                            f"Codex action dispatch failed: {error}",
                            file=sys.stderr,
                            flush=True,
                        )

            state_thread = threading.Thread(
                target=poll_state,
                name="d200-state-poller",
                daemon=True,
            )
            action_thread = threading.Thread(
                target=dispatch_actions,
                name="d200-action-dispatcher",
                daemon=True,
            )
            render_thread = threading.Thread(
                target=render_profiles,
                name="d200-profile-renderer",
                daemon=True,
            )
            cache_thread = threading.Thread(
                target=build_profile_cache,
                name="d200-profile-cache",
                daemon=True,
            )
            action_thread.start()
            if output_writes:
                state_thread.start()
                render_thread.start()
                cache_thread.start()
            else:
                state_thread = None
                render_thread = None
                cache_thread = None
                state_ready.set()

            if restore_after_reconnect:
                recovery_profile = cached_profile or make_recovery_profile()
                print(
                    f"D200 restoring profile after USB reconnect ({len(recovery_profile)} bytes)…",
                    flush=True,
                )
                device.upload(recovery_profile)
                save_cached_profile(recovery_profile)
                device.keep_alive()
                last_keep_alive = time.monotonic()
                profile_restore_required = False
                print("D200 firmware display and clock restored.", flush=True)
            elif output_writes and not applied_digest[0]:
                print("D200 setting initial brightness…", flush=True)
                device.set_brightness(100)
                time.sleep(COMMAND_SETTLE_SECONDS)
                print("D200 enabling firmware clock…", flush=True)
                device.keep_alive()
                time.sleep(COMMAND_SETTLE_SECONDS)
                print("D200 initial commands accepted.", flush=True)
            elif output_writes:
                print("D200 restoring cached profile display layer…", flush=True)
                device.keep_alive()
                last_keep_alive = time.monotonic()
                print("D200 cached profile active; output writes deferred.", flush=True)
            else:
                device.keep_alive()
                last_keep_alive = time.monotonic()
                print(
                    "D200 diagnostic input mode active; profile writes disabled.",
                    flush=True,
                )
            upload_reports = None
            upload_digest = None
            upload_profile = None
            upload_thread_routes: list[dict] = []
            upload_key_digests: dict[int, str] = {}
            upload_icons: dict[int, bytes] = {}
            upload_partial = False
            upload_size = 0
            upload_packet_index = 0
            activation_due = None
            activation_cache: tuple[str, dict[int, bytes]] | None = None
            upload_changed_at = None
            while not stopped:
                now = time.monotonic()
                event = device.read_button(
                    1 if upload_reports is not None else 50
                )
                if event:
                    index, pressed = event
                    last_button_at = now
                    print(
                        f"D200 button {index} {'down' if pressed else 'up'}.",
                        flush=True,
                    )
                    should_dispatch = (
                        (pressed and index < ACTIVE_SLOTS)
                        or index in ACTION_KEYS
                        or (pressed and index == USAGE_DISPLAY_KEY)
                        or (pressed and index == FOCUS_KEY)
                    )
                    if should_dispatch:
                        route = (
                            active_thread_routes[index].copy()
                            if pressed
                            and index < ACTIVE_SLOTS
                            and index < len(active_thread_routes)
                            else None
                        )
                        action_events.put((index, pressed, now, route))
                    # Give the Codex dispatcher the GIL immediately and never
                    # start a synchronous HID write in the same iteration as
                    # physical input. This is the strict hot-path priority rule.
                    time.sleep(0)
                    continue

                if (
                    output_writes
                    and
                    upload_reports is None
                    and activation_due is None
                ):
                    try:
                        (
                            candidate_base_digest,
                            candidate_digest,
                            profile,
                            candidate_thread_routes,
                            candidate_key_digests,
                            candidate_partial,
                            candidate_changed_at,
                            candidate_icons,
                        ) = prepared_profiles.get_nowait()
                    except queue.Empty:
                        candidate_digest = None
                    if candidate_digest is not None:
                        with refresh_condition:
                            candidate_is_current = (
                                candidate_base_digest == applied_digest[0]
                                and
                                candidate_digest == target[0]["digest"]
                                and candidate_digest != applied_digest[0]
                            )
                            if candidate_base_digest != applied_digest[0]:
                                refresh_condition.notify_all()
                        if candidate_is_current:
                            if not profile:
                                applied_digest[0] = candidate_digest
                                active_thread_routes[:] = (
                                    candidate_thread_routes
                                )
                                applied_key_digests.clear()
                                applied_key_digests.update(
                                    candidate_key_digests
                                )
                                runtime_applied_digest = applied_digest[0]
                                save_cached_digest(
                                    applied_digest[0],
                                    [
                                        str(route.get("threadKey") or "")
                                        for route in active_thread_routes
                                    ],
                                    applied_key_digests,
                                )
                                print(
                                    "D200 display already matches; "
                                    "button mapping committed without HID output.",
                                    flush=True,
                                )
                                continue
                            upload_size = len(profile)
                            upload_profile = profile
                            upload_thread_routes = candidate_thread_routes
                            upload_key_digests = candidate_key_digests
                            upload_icons = candidate_icons
                            upload_partial = candidate_partial
                            upload_reports = iter(
                                device.profile_reports(
                                    profile,
                                    partial=candidate_partial,
                                )
                            )
                            upload_digest = candidate_digest
                            upload_changed_at = candidate_changed_at
                            upload_packet_index = 0
                            print(
                                "D200 uploading "
                                f"{'partial-key' if candidate_partial else 'full'} "
                                f"profile ({upload_size} bytes), "
                                f"{(now - candidate_changed_at) * 1000:.0f}ms "
                                "after detection…",
                                flush=True,
                            )

                if upload_reports is not None:
                    try:
                        next_report = next(upload_reports)
                        device.write(
                            next_report,
                            f"profile packet {upload_packet_index + 1}",
                        )
                        upload_packet_index += 1
                        time.sleep(UPLOAD_PACKET_DELAY_SECONDS)
                    except StopIteration:
                        upload_reports = None
                        activation_due = time.monotonic() + COMMAND_SETTLE_SECONDS
                        print(
                            f"D200 profile transferred ({upload_size} bytes); "
                            "activating display layer…",
                            flush=True,
                        )
                    except OSError as error:
                        raise OSError(
                            f"profile packet {upload_packet_index + 1} failed: {error}"
                        ) from error
                elif activation_due is not None and now >= activation_due:
                    device.keep_alive()
                    last_keep_alive = time.monotonic()
                    activation_due = None
                    # The image resources, logical digest and button mapping
                    # become one committed framebuffer only after firmware
                    # activation succeeds. Never expose a new mapping while
                    # the old keys are still visible.
                    applied_digest[0] = upload_digest
                    active_thread_routes[:] = upload_thread_routes
                    applied_key_digests.clear()
                    applied_key_digests.update(upload_key_digests)
                    runtime_applied_digest = applied_digest[0]
                    save_cached_digest(
                        applied_digest[0],
                        [
                            str(route.get("threadKey") or "")
                            for route in active_thread_routes
                        ],
                        applied_key_digests,
                    )
                    if upload_partial and upload_digest is not None:
                        activation_cache = (
                            upload_digest,
                            upload_icons.copy(),
                        )
                    elif upload_profile is not None:
                        save_cached_profile(upload_profile)
                    print(
                        f"D200 profile visible at {time.strftime('%H:%M:%S')}; "
                        "physical display latency "
                        f"{((time.monotonic() - upload_changed_at) * 1000 if upload_changed_at is not None else 0):.0f}ms.",
                        flush=True,
                    )
                    upload_changed_at = None
                    upload_digest = None
                    upload_profile = None
                    upload_thread_routes = []
                    upload_key_digests = {}
                    upload_icons = {}
                    upload_partial = False
                    with refresh_condition:
                        refresh_condition.notify_all()
                    if activation_cache is not None:
                        while True:
                            try:
                                cache_profiles.get_nowait()
                            except queue.Empty:
                                break
                        cache_profiles.put_nowait(activation_cache)
                        activation_cache = None
                    if once:
                        return
                elif (
                    now - last_keep_alive >= KEEP_ALIVE_SECONDS
                    and now - last_button_at >= 2.0
                    and action_events.empty()
                ):
                    device.keep_alive()
                    last_keep_alive = now

                if (
                    once
                    and state_ready.is_set()
                    and upload_reports is None
                    and activation_due is None
                    and prepared_profiles.empty()
                ):
                    with refresh_condition:
                        if target[0]["digest"] == applied_digest[0]:
                            return

                if now - stable_since >= 60:
                    reconnect_attempt = 0
                    stable_since = now
                # read_button already blocks while idle. A small upload sleep
                # still yields to the state and action threads between reports.
                if upload_reports is not None:
                    time.sleep(0.001)
        except OSError as error:
            reconnect_attempt += 1
            profile_restore_required = True
            if once:
                raise
            retry_delay = reconnect_delay(error, reconnect_attempt)
            message = str(error)
            now = time.monotonic()
            if message != last_connection_error or now - last_connection_log_at >= 10:
                print(
                    f"D200 connection lost: {error}; "
                    f"reconnecting in {retry_delay:.1f}s.",
                    file=sys.stderr,
                    flush=True,
                )
                last_connection_error = message
                last_connection_log_at = now
        finally:
            session_stop.set()
            if state_thread is not None:
                state_thread.join(timeout=1)
            if action_thread is not None:
                action_thread.join(timeout=1)
            if render_thread is not None:
                with refresh_condition:
                    refresh_condition.notify_all()
                render_thread.join(timeout=1)
            if cache_thread is not None:
                cache_thread.join(timeout=1)
            if agent_executor is not None:
                agent_executor.shutdown(wait=False, cancel_futures=True)
            if device is not None:
                try:
                    device.close()
                except OSError:
                    pass
        if retry_delay and not stopped:
            time.sleep(retry_delay)
    native_adapter.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--self-test",
        action="store_true",
        help="validate rendering, profile ZIP and HID framing without USB or Codex",
    )
    mode.add_argument(
        "--diagnose",
        action="store_true",
        help="open the D200 once and print its HID endpoint; never writes to it",
    )
    mode.add_argument(
        "--refresh-profile",
        action="store_true",
        help="explicitly upload one compressed profile, activate it, and exit",
    )
    mode.add_argument(
        "--state",
        "--native-state",
        action="store_true",
        help="print one Codex app-server snapshot without opening the D200",
    )
    args = parser.parse_args()
    try:
        if args.self_test:
            print(json.dumps(bare_metal_self_test(), ensure_ascii=False))
            return 0
        if args.diagnose:
            print(json.dumps(diagnose_device(), ensure_ascii=False, default=str))
            return 0
        if args.state:
            from native_codex import NativeCodex

            adapter = NativeCodex()
            try:
                deadline = time.time() + 12
                revision = -1
                state = adapter.snapshot()
                while not state.get("connected") and time.time() < deadline:
                    revision, state = adapter.wait_for_change(
                        revision,
                        timeout=max(0.0, deadline - time.time()),
                    )
                print(json.dumps(state, ensure_ascii=False, default=str))
                return 0 if state.get("connected") else 1
            finally:
                adapter.close()
        refresh_profile = args.refresh_profile
        run(
            once=refresh_profile,
            output_writes=refresh_profile or OUTPUT_WRITES_ENABLED,
            force_profile=refresh_profile,
        )
        return 0
    except Exception as error:
        print(f"openCodexMicro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
