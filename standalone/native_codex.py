"""Native Codex adapter used by the D200 daemon.

The adapter talks to the bundled Codex app-server for inventory and account
usage, watches rollout JSONL with macOS kernel events for the Desktop task
lifecycle, and uses the official ``codex://threads/<id>`` deep link for task
switching.  An optional loopback-only Codex bridge uses the same internal
Micro event bus as the official keyboard; native navigation remains available
when Codex was launched normally.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import resource
import select
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import (
    ProxyHandler,
    Request,
    build_opener,
)


CODEX_BINARY_CANDIDATES = (
    Path("/Applications/Codex.app/Contents/Resources/codex"),
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
)
APP_ROOT = (
    Path.home()
    / "Library"
    / "Application Support"
    / "openCodexMicro"
)
SEEN_PATH = APP_ROOT / "native-seen.json"
KEYBINDINGS_PATH = Path.home() / ".codex" / "keybindings.json"
GLOBAL_STATE_PATH = Path.home() / ".codex" / ".codex-global-state.json"
BRIDGE_URL = "http://127.0.0.1:17373"
BRIDGE_ACTIONS = frozenset({
    "fast",
    "pin",
    "new",
    "fork",
    "mic",
    "steer",
    "submit",
})
SLOT_COUNT = 5
MAX_WATCHED_ROLLOUTS = 2048
APP_SERVER_NOTIFICATION_METHODS = {
    "$/closed",
    "account/rateLimits/updated",
    "turn/started",
    "turn/completed",
    "thread/name/updated",
    "thread/started",
    "thread/archived",
    "thread/deleted",
    "thread/unarchived",
}
USAGE_SECONDS = 600.0
INVENTORY_DEBOUNCE_SECONDS = 0.150
NEW_ROLLOUT_QUIET_SECONDS = 0.050
STARTUP_USAGE_QUIET_SECONDS = 2.0
RECENT_ROLLOUT_BYTES = 2 * 1024 * 1024
ROLLOUT_NAME = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)
THREAD_UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
THREAD_KEY = re.compile(
    rf"(?:{THREAD_UUID_PATTERN}|client-new-thread:{THREAD_UUID_PATTERN})"
)

REMOTE_WATCH_SCRIPT = r'''
from __future__ import print_function
import base64
import ctypes
import json
import os
import select
import sqlite3
import struct
import sys
import time

home = os.path.expanduser("~")
sessions = os.path.join(home, ".codex", "sessions")
database = os.path.join(home, ".codex", "state_5.sqlite")
database_dir = os.path.dirname(database)
recent_bytes = 2 * 1024 * 1024
offsets = {}
libc = ctypes.CDLL(None, use_errno=True)
fd = libc.inotify_init1(os.O_CLOEXEC)
if fd < 0:
    raise OSError(ctypes.get_errno(), "inotify_init1")
mask = 0x00000002 | 0x00000008 | 0x00000080 | 0x00000100 | 0x00000400
watches = {}
last_inventory = None
inventory_failed = False
LIFECYCLE_MARKERS = (
    b'"type":"task_started"',
    b'"type":"task_complete"',
    b'"type":"turn_aborted"',
    b'"type":"task_failed"',
    b'"type":"turn_failed"',
    b'"type":"stream_error"',
)

def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()

def inventory():
    global inventory_failed, last_inventory
    try:
        connection = sqlite3.connect(
            "file:" + database + "?mode=ro", uri=True, timeout=1
        )
        rows = connection.execute(
            """
            SELECT id, rollout_path, title, name, preview,
                   first_user_message, cwd,
                   CASE WHEN recency_at_ms > 0
                        THEN recency_at_ms ELSE updated_at * 1000 END
            FROM threads
            WHERE archived = 0
              AND COALESCE(thread_source, 'user') = 'user'
            ORDER BY recency_at_ms DESC, updated_at DESC
            LIMIT 2048
            """
        ).fetchall()
        connection.close()
        inventory_failed = False
    except Exception as error:
        inventory_failed = True
        emit({
            "type": "warning",
            "message": str(error),
            "sqliteVersion": sqlite3.sqlite_version,
            "mode": "rollout-only",
        })
        return last_inventory or []
    threads = []
    for row in rows:
        threads.append({
            "id": row[0],
            "path": row[1],
            "title": row[2] or "",
            "name": row[3] or "",
            "preview": row[4] or "",
            "firstUserMessage": row[5] or "",
            "cwd": row[6] or "",
            "recencyAt": int(row[7] or 0),
            "status": {"type": "notLoaded"},
        })
    if threads != last_inventory:
        emit({"type": "inventory", "threads": threads})
        last_inventory = threads
    return threads

def last_lifecycle(path, limit):
    position = limit
    carry = b""
    try:
        with open(path, "rb") as source:
            while position > 0:
                start = max(0, position - 256 * 1024)
                source.seek(start)
                data = source.read(position - start) + carry
                lines = data.split(b"\n")
                carry = lines.pop(0) if start else b""
                for line in reversed(lines):
                    if any(marker in line for marker in LIFECYCLE_MARKERS):
                        return line
                position = start
    except OSError:
        pass
    return b""

def send_delta(path, initial=False):
    try:
        size = os.path.getsize(path)
        previous = offsets.get(path)
        reset = previous is None or size < previous
        start = max(0, size - recent_bytes) if initial or reset else previous
        with open(path, "rb") as source:
            source.seek(start)
            data = source.read()
            offsets[path] = source.tell()
        if start:
            seed = last_lifecycle(path, start)
            if seed:
                data = seed + b"\n" + data
        if data:
            emit({
                "type": "rollout",
                "path": path,
                "reset": bool(reset),
                "data": base64.b64encode(data).decode("ascii"),
            })
    except OSError:
        return

def add_watch(path):
    encoded = os.fsencode(path)
    descriptor = libc.inotify_add_watch(fd, encoded, mask)
    if descriptor >= 0:
        watches[descriptor] = path

def add_tree(root):
    if not os.path.isdir(root):
        return
    for current, directories, _files in os.walk(root):
        add_watch(current)

add_tree(sessions)
add_watch(database_dir)
threads = inventory()
for thread in threads:
    path = thread.get("path")
    if path:
        send_delta(path, initial=True)
if inventory_failed:
    recent_rollouts = []
    for current, _directories, files in os.walk(sessions):
        for name in files:
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            path = os.path.join(current, name)
            try:
                recent_rollouts.append((os.path.getmtime(path), path))
            except OSError:
                pass
    recent_rollouts.sort(reverse=True)
    for _modified, path in recent_rollouts[:2048]:
        send_delta(path, initial=True)
emit({"type": "online"})

event_header = struct.Struct("iIII")
inventory_due = None
while True:
    timeout = (
        None
        if inventory_due is None
        else max(0.0, inventory_due - time.monotonic())
    )
    readable, _, _ = select.select([fd], [], [], timeout)
    if not readable:
        if (
            inventory_due is not None
            and time.monotonic() >= inventory_due
        ):
            inventory()
            inventory_due = None
        continue
    payload = os.read(fd, 1024 * 1024)
    position = 0
    inventory_needed = False
    while position + event_header.size <= len(payload):
        watch, event_mask, _cookie, length = event_header.unpack_from(
            payload, position
        )
        position += event_header.size
        raw_name = payload[position:position + length]
        position += length
        name = raw_name.split(b"\0", 1)[0].decode("utf-8", "replace")
        parent = watches.get(watch)
        if parent is None:
            continue
        path = os.path.join(parent, name) if name else parent
        if event_mask & 0x40000000:
            if event_mask & (0x00000080 | 0x00000100):
                add_tree(path)
            continue
        if name in {
            "state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm"
        }:
            inventory_needed = True
        if os.path.basename(path).startswith("rollout-") and path.endswith(".jsonl"):
            send_delta(path)
    if inventory_needed and inventory_due is None:
        # SQLite WAL activity can generate hundreds of inotify events per
        # second. Reconcile at most four times per second, then emit only when
        # the inventory value actually changed.
        inventory_due = time.monotonic() + 0.25
'''


def remote_hosts_from_state(path: Path = GLOBAL_STATE_PATH) -> dict[str, str]:
    """Return Codex-managed SSH host IDs mapped to concrete SSH aliases."""
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    result = {}
    for connection in state.get("codex-managed-remote-connections") or []:
        if not isinstance(connection, dict):
            continue
        host_id = connection.get("hostId")
        alias = connection.get("alias")
        if (
            isinstance(host_id, str)
            and host_id.startswith("remote-ssh-")
            and isinstance(alias, str)
            and alias
        ):
            result[host_id] = alias
    return result


def thread_assignments(path: Path = GLOBAL_STATE_PATH) -> dict[str, dict]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    assignments = state.get("thread-project-assignments")
    return assignments if isinstance(assignments, dict) else {}


def desktop_settings(config_path: Path | None = None) -> dict:
    """Read the Desktop's own settings; missing values keep Codex defaults."""
    path = config_path or (Path.home() / ".codex" / "config.toml")
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    desktop = config.get("desktop")
    return desktop if isinstance(desktop, dict) else {}


def composer_enter_behavior(config_path: Path | None = None) -> str:
    desktop = desktop_settings(config_path)
    value = desktop.get(
        "composerEnterBehavior",
        desktop.get("composer_enter_behavior"),
    )
    return str(value) if value in {"enter", "cmdIfMultiline", "cmdAlways"} else "enter"


def shortcut_script(shortcut: str) -> str:
    """Translate a Codex Desktop shortcut string into a macOS key event."""
    parts = [part.strip() for part in shortcut.split("+") if part.strip()]
    if not parts:
        raise ValueError("Empty Codex shortcut")
    key = parts[-1]
    modifier_names = {
        "command": "command down",
        "cmd": "command down",
        "cmdorctrl": "command down",
        "alt": "option down",
        "option": "option down",
        "ctrl": "control down",
        "control": "control down",
        "shift": "shift down",
    }
    unsupported_modifiers = [
        part for part in parts[:-1] if part.lower() not in modifier_names
    ]
    if unsupported_modifiers:
        raise ValueError(
            "Unsupported Codex shortcut modifier: "
            + ", ".join(unsupported_modifiers)
        )
    modifiers = [
        modifier_names[part.lower()]
        for part in parts[:-1]
        if part.lower() in modifier_names
    ]
    if key.lower() in {"enter", "return"}:
        event = "key code 36"
    elif len(key) == 1:
        event = f'keystroke {json.dumps(key.lower())}'
    else:
        raise ValueError(f"Unsupported Codex shortcut key: {key}")
    if len(modifiers) == 1:
        return f"{event} using {modifiers[0]}"
    if modifiers:
        return f"{event} using {{{', '.join(modifiers)}}}"
    return event


def configured_shortcuts(
    keybindings_path: Path | None = None,
) -> dict[str, str]:
    path = keybindings_path or KEYBINDINGS_PATH
    try:
        entries = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    result = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        command = entry.get("command")
        shortcut = entry.get("key")
        if isinstance(command, str) and isinstance(shortcut, str):
            result[command] = shortcut
    return result


def command_key_script(
    command: str,
    fallback: str,
    keybindings_path: Path | None = None,
) -> str:
    shortcut = configured_shortcuts(keybindings_path).get(command)
    if shortcut:
        try:
            return shortcut_script(shortcut)
        except ValueError:
            pass
    return fallback


def submit_key_script(config_path: Path | None = None) -> str:
    if composer_enter_behavior(config_path) == "enter":
        return "key code 36"
    return "key code 36 using command down"


def dispatch_desktop_action(action: str) -> None:
    """Focus Codex and dispatch an action using its configured shortcut."""
    if action == "focus":
        subprocess.Popen(
            ["/usr/bin/open", "-b", "com.openai.codex"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    key_script = {
        "submit": command_key_script(
            "composer.submit",
            submit_key_script(),
        ),
        "stop": "key code 53",
        "term": "key code 50 using control down",
        "pin": command_key_script(
            "toggleThreadPin",
            'keystroke "p" using {command down, option down}',
        ),
        "new": command_key_script(
            "newTask",
            'keystroke "n" using command down',
        ),
        "fork": command_key_script(
            "forkThread",
            'keystroke "f" using {command down, option down}',
        ),
        "fast": command_key_script(
            "composer.toggleFastMode",
            'keystroke "t" using {command down, option down}',
        ),
        "mic": command_key_script(
            "realtimeVoice.toggleMicrophoneMute",
            'keystroke "m" using {command down, option down}',
        ),
    }.get(action)
    if key_script is None:
        raise ValueError(f"Unsupported Codex desktop action: {action}")
    script = f"""
tell application id "com.openai.codex" to activate
tell application "System Events"
  tell first application process whose bundle identifier is "com.openai.codex"
    set frontmost to true
    {key_script}
  end tell
end tell
"""
    subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def dispatch_bridge_thread(
    thread_id: str,
    slot: int = 0,
    bridge_url: str = BRIDGE_URL,
) -> bool:
    """Send a task key through the optional official-Micro renderer bridge."""
    normalized = str(thread_id).removeprefix("local:")
    if not THREAD_KEY.fullmatch(normalized):
        return False
    url = (
        f"{bridge_url}/thread/{quote(normalized, safe='')}/click"
        f"?slot={max(0, min(SLOT_COUNT - 1, int(slot)))}"
    )
    try:
        request = Request(url, method="POST")
        with build_opener(ProxyHandler({})).open(
            request,
            timeout=3.0,
        ) as response:
            payload = json.loads(response.read())
        return payload.get("ok") is True and payload.get("bridge") is True
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        print(
            f"Codex bridge task dispatch failed: {error}",
            file=sys.stderr,
            flush=True,
        )
        return False


def dispatch_bridge_action(
    action: str,
    pressed: bool,
    bridge_url: str = BRIDGE_URL,
) -> bool:
    """Send an action through the renderer bridge with explicit focus."""
    if action not in BRIDGE_ACTIONS:
        return False
    if action in {"pin", "new"} and not pressed:
        return True
    phase = "down" if pressed else "up"
    try:
        request = Request(
            f"{bridge_url}/action/{quote(action, safe='')}/{phase}",
            method="POST",
        )
        with build_opener(ProxyHandler({})).open(
            request,
            timeout=1.2,
        ) as response:
            payload = json.loads(response.read())
        return payload.get("ok") is True
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        print(
            f"Codex bridge {action} dispatch failed: {error}",
            file=sys.stderr,
            flush=True,
        )
        return False


def open_remote_thread_from_dock(title: str) -> None:
    """Use Codex's native Recent menu, whose callbacks are host-aware."""
    if not title:
        raise RuntimeError("Remote Codex task has no title for Dock Recent")
    script = r'''
on run argv
  set targetTitle to item 1 of argv
  tell application "System Events"
    tell process "Dock"
      set dockItem to UI element "ChatGPT" of list 1
      perform action "AXShowMenu" of dockItem
      set dockMenu to first UI element of dockItem whose role is "AXMenu"
      set matches to every menu item of dockMenu whose name is targetTitle and enabled is true
      set topCount to count of matches
      if topCount is 1 then
        click item 1 of matches
        return
      end if
      if topCount > 1 then error "Ambiguous Codex task title"
      set moreItems to every menu item of dockMenu whose name is "More"
      if (count of moreItems) is 0 then error "Codex task is not in the Recent menu"
      set moreItem to item 1 of moreItems
      perform action "AXShowMenu" of moreItem
      set moreMenu to first UI element of moreItem whose role is "AXMenu"
      set moreMatches to every menu item of moreMenu whose name is targetTitle and enabled is true
      if (count of moreMatches) is not 1 then error "Codex task title is missing or ambiguous"
      click item 1 of moreMatches
    end tell
  end tell
end run
'''
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, "--", title],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
    )


def show_native_navigation_notice() -> None:
    """Explain why native fallback was used instead of the fast bridge."""
    script = '''
display alert "Codex Bridge is not active" message "This Codex instance was not started by Codex Bridge. Local task keys use codex:// links; remote and temporary task keys require Codex Bridge. For direct, host-aware switching, quit Codex and open Codex Bridge.app from ~/Applications." as informational
'''
    subprocess.Popen(
        ["/usr/bin/osascript", "-e", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def find_codex_binary() -> str:
    configured = os.environ.get(
        "OPEN_CODEX_MICRO_CODEX_BIN",
        os.environ.get("CODEX_KEYBOARD_CODEX_BIN"),
    )
    if configured:
        return configured
    for candidate in CODEX_BINARY_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    result = subprocess.run(
        ["/usr/bin/which", "codex"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class AppServerClient:
    """Small persistent JSONL client for the official Codex app-server."""

    def __init__(self, binary: str):
        self.process = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env={**os.environ, "RUST_LOG": "error"},
        )
        self._lock = threading.Lock()
        self._pending: dict[int, queue.Queue] = {}
        self._next_id = 0
        self._notifications: list[Callable[[dict], None]] = []
        self._closing = threading.Event()
        self._reader = threading.Thread(
            target=self._read_messages,
            name="codex-app-server-reader",
            daemon=True,
        )
        self._reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "open_codex_micro",
                    "title": "openCodexMicro",
                    "version": "0.3.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")

    def on_notification(self, callback: Callable[[dict], None]) -> None:
        self._notifications.append(callback)

    def _read_messages(self) -> None:
        assert self.process.stdout is not None
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            request_id = message.get("id")
            if request_id is not None:
                with self._lock:
                    result_queue = self._pending.pop(request_id, None)
                if result_queue is not None:
                    result_queue.put(message)
                continue
            for callback in tuple(self._notifications):
                try:
                    callback(message)
                except Exception:
                    continue
        if not self._closing.is_set():
            for callback in tuple(self._notifications):
                try:
                    callback({"method": "$/closed", "params": {}})
                except Exception:
                    continue
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for result_queue in pending:
            result_queue.put(
                {"error": {"message": "Codex app-server exited unexpectedly"}}
            )

    def _send(self, message: dict) -> None:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("Codex app-server is not running")
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 10.0,
    ) -> dict:
        result_queue: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = result_queue
        try:
            self._send({"method": method, "id": request_id, "params": params or {}})
            message = result_queue.get(timeout=timeout)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if message.get("error"):
            raise RuntimeError(
                message["error"].get("message") or f"{method} failed"
            )
        return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def close(self) -> None:
        self._closing.set()
        if self.process.poll() is not None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        self.process.terminate()


class RolloutTail:
    """Incrementally derive Desktop lifecycle state from one rollout file."""

    def __init__(
        self,
        path: str | Path,
        start_at_end: bool = False,
        recent_bytes: int | None = None,
    ):
        self.path = Path(path)
        try:
            size = self.path.stat().st_size
            if start_at_end:
                self.offset = size
            elif recent_bytes is not None:
                self.offset = max(0, size - recent_bytes)
            else:
                self.offset = 0
        except OSError:
            self.offset = 0
        self.remainder = b""
        self.started_at = 0
        self.completed_at = 0
        self.error_at = 0
        self.pending_input: set[str] = set()
        self.pending_explicit_input: set[str] = set()
        if self.offset:
            seed = self._last_lifecycle_before(self.offset)
            if seed:
                self.feed(seed + b"\n")

    def _last_lifecycle_before(self, limit: int) -> bytes:
        markers = (
            b'"type":"task_started"',
            b'"type": "task_started"',
            b'"type":"task_complete"',
            b'"type": "task_complete"',
            b'"type":"turn_aborted"',
            b'"type": "turn_aborted"',
            b'"type":"task_failed"',
            b'"type": "task_failed"',
            b'"type":"turn_failed"',
            b'"type": "turn_failed"',
            b'"type":"stream_error"',
            b'"type": "stream_error"',
        )
        position = limit
        carry = b""
        try:
            with self.path.open("rb") as source:
                while position > 0:
                    start = max(0, position - 256 * 1024)
                    source.seek(start)
                    data = source.read(position - start) + carry
                    lines = data.split(b"\n")
                    carry = lines.pop(0) if start else b""
                    for line in reversed(lines):
                        if any(marker in line for marker in markers):
                            return line
                    position = start
        except OSError:
            pass
        return b""

    @staticmethod
    def _timestamp(record: dict) -> int:
        try:
            return int(
                datetime.fromisoformat(
                    record.get("timestamp", "").replace("Z", "+00:00")
                ).timestamp()
                * 1000
            )
        except (TypeError, ValueError):
            return int(time.time() * 1000)

    @staticmethod
    def _requires_input(payload: dict) -> bool:
        name = str(payload.get("name") or "")
        if name == "request_user_input":
            return True
        arguments = payload.get("arguments", payload.get("input", ""))
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments, separators=(",", ":"))
            except TypeError:
                return False
        return (
            "require_escalated" in arguments
            and "sandbox_permissions" in arguments
        )

    def update(self) -> bool:
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size < self.offset:
            self.offset = 0
            self.remainder = b""
            self.started_at = 0
            self.completed_at = 0
            self.error_at = 0
            self.pending_input.clear()
            self.pending_explicit_input.clear()
        if size == self.offset:
            return False
        with self.path.open("rb") as source:
            source.seek(self.offset)
            chunk = source.read()
            self.offset = source.tell()
        return self.feed(chunk)

    def feed(self, chunk: bytes, reset: bool = False) -> bool:
        """Parse a streamed append, allowing remote inotify to reuse lifecycle."""
        if reset:
            self.remainder = b""
            self.started_at = 0
            self.completed_at = 0
            self.error_at = 0
            self.pending_input.clear()
            self.pending_explicit_input.clear()
        before = (
            self.started_at,
            self.completed_at,
            self.error_at,
            frozenset(self.pending_input),
            frozenset(self.pending_explicit_input),
        )
        records = (self.remainder + chunk).split(b"\n")
        self.remainder = records.pop()
        for raw in records:
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            record_type = record.get("type")
            payload = record.get("payload") or {}
            timestamp = self._timestamp(record)
            if record_type == "event_msg":
                event_type = payload.get("type")
                if event_type == "task_started":
                    self.started_at = max(self.started_at, timestamp)
                    self.error_at = 0
                elif event_type == "task_complete":
                    self.completed_at = max(self.completed_at, timestamp)
                    self.pending_input.clear()
                    self.pending_explicit_input.clear()
                    if payload.get("error") or payload.get("status") in {
                        "failed",
                        "error",
                    }:
                        self.error_at = max(self.error_at, timestamp)
                elif event_type == "turn_aborted":
                    self.completed_at = max(self.completed_at, timestamp)
                    self.pending_input.clear()
                    self.pending_explicit_input.clear()
                    if payload.get("reason") not in {
                        "interrupted",
                        "user_cancelled",
                    }:
                        self.error_at = max(self.error_at, timestamp)
                elif event_type in {
                    "error",
                    "stream_error",
                    "task_failed",
                    "turn_failed",
                }:
                    self.error_at = max(self.error_at, timestamp)
                continue
            if record_type != "response_item":
                continue
            item_type = payload.get("type")
            call_id = str(payload.get("call_id") or "")
            if (
                item_type in {"function_call", "custom_tool_call"}
                and call_id
                and self._requires_input(payload)
            ):
                self.pending_input.add(call_id)
                if str(payload.get("name") or "") == "request_user_input":
                    self.pending_explicit_input.add(call_id)
            elif (
                item_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_search_output",
                }
                and call_id
            ):
                self.pending_input.discard(call_id)
                self.pending_explicit_input.discard(call_id)
        return before != (
            self.started_at,
            self.completed_at,
            self.error_at,
            frozenset(self.pending_input),
            frozenset(self.pending_explicit_input),
        )

    @classmethod
    def streamed(cls, path: str | Path) -> "RolloutTail":
        tail = cls.__new__(cls)
        tail.path = Path(path)
        tail.offset = 0
        tail.remainder = b""
        tail.started_at = 0
        tail.completed_at = 0
        tail.error_at = 0
        tail.pending_input = set()
        tail.pending_explicit_input = set()
        return tail


class RolloutWatcher:
    """Wake the native adapter when macOS reports rollout filesystem changes."""

    def __init__(self, callback: Callable[[Path, bool, int], None]):
        if not hasattr(select, "kqueue"):
            raise RuntimeError("native rollout events require macOS kqueue")
        self._callback = callback
        self._lock = threading.Lock()
        self._configured = threading.Condition(self._lock)
        self._generation = 0
        self._applied_generation = -1
        self._files: tuple[Path, ...] = ()
        self._directories: tuple[Path, ...] = ()
        self._stopped = threading.Event()
        self._read_fd, self._write_fd = os.pipe()
        os.set_blocking(self._read_fd, False)
        os.set_blocking(self._write_fd, False)
        self._thread = threading.Thread(
            target=self._run,
            name="codex-rollout-watcher",
            daemon=True,
        )
        self._thread.start()

    def configure(self, files: list[Path]) -> None:
        selected = tuple(
            dict.fromkeys(path.resolve() for path in files)
        )[:MAX_WATCHED_ROLLOUTS]
        normalized = tuple(sorted(selected))
        directories: set[Path] = set()
        for path in normalized:
            parent = path.parent
            while True:
                directories.add(parent)
                if parent.name == "sessions" or parent.parent == parent:
                    break
                parent = parent.parent
        required_fds = len(normalized) + len(directories) + 8
        soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        if required_fds > soft_limit:
            resource.setrlimit(
                resource.RLIMIT_NOFILE,
                (min(required_fds, hard_limit), hard_limit),
            )
        with self._lock:
            if normalized == self._files and tuple(sorted(directories)) == self._directories:
                return
            self._files = normalized
            self._directories = tuple(sorted(directories))
            self._generation += 1
            generation = self._generation
        self._wake()
        with self._configured:
            self._configured.wait_for(
                lambda: (
                    self._applied_generation >= generation
                    or self._stopped.is_set()
                ),
                timeout=2,
            )

    def _wake(self) -> None:
        try:
            os.write(self._write_fd, b"x")
        except (BlockingIOError, OSError):
            pass

    def _run(self) -> None:
        kernel_queue = select.kqueue()
        open_files: dict[int, tuple[Path, bool]] = {}
        open_targets: dict[tuple[Path, bool], int] = {}
        control = select.kevent(
            self._read_fd,
            filter=select.KQ_FILTER_READ,
            flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE,
        )
        kernel_queue.control([control], 0, 0)

        def close_watches() -> None:
            for descriptor in tuple(open_files):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            open_files.clear()
            open_targets.clear()

        def rebuild() -> None:
            with self._lock:
                targets = {
                    *((path, False) for path in self._files),
                    *((path, True) for path in self._directories),
                }
            existing = set(open_targets)
            additions = targets - existing
            removals = existing - targets
            changes = []
            note_flags = (
                select.KQ_NOTE_WRITE
                | select.KQ_NOTE_EXTEND
                | select.KQ_NOTE_DELETE
                | select.KQ_NOTE_RENAME
                | select.KQ_NOTE_REVOKE
            )
            added: list[tuple[int, tuple[Path, bool]]] = []
            for path, is_directory in additions:
                try:
                    descriptor = os.open(
                        path,
                        getattr(os, "O_EVTONLY", os.O_RDONLY),
                    )
                except OSError:
                    continue
                open_files[descriptor] = (path, is_directory)
                open_targets[(path, is_directory)] = descriptor
                added.append((descriptor, (path, is_directory)))
                changes.append(
                    select.kevent(
                        descriptor,
                        filter=select.KQ_FILTER_VNODE,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                        fflags=note_flags,
                    )
                )
            try:
                if changes:
                    kernel_queue.control(changes, 0, 0)
            except OSError:
                for descriptor, target in added:
                    open_files.pop(descriptor, None)
                    open_targets.pop(target, None)
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise
            # Register replacements before removing old watches. This both
            # avoids the historical event-loss window and turns the common
            # one-thread recent change into O(1) fd work.
            for target in removals:
                descriptor = open_targets.pop(target, None)
                if descriptor is None:
                    continue
                open_files.pop(descriptor, None)
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            with self._configured:
                self._applied_generation = self._generation
                self._configured.notify_all()

        try:
            rebuild()
            while not self._stopped.is_set():
                for event in kernel_queue.control(None, 32, None):
                    if event.ident == self._read_fd:
                        try:
                            while os.read(self._read_fd, 4096):
                                pass
                        except BlockingIOError:
                            pass
                        if not self._stopped.is_set():
                            rebuild()
                        continue
                    target = open_files.get(event.ident)
                    if target is not None:
                        path, is_directory = target
                        self._callback(path, is_directory, event.fflags)
        finally:
            close_watches()
            kernel_queue.close()

    def close(self) -> None:
        self._stopped.set()
        with self._configured:
            self._configured.notify_all()
        self._wake()
        self._thread.join(timeout=2)
        for descriptor in (self._read_fd, self._write_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


class GlobalStateWatcher:
    """Watch Codex's atomically-replaced global state without polling."""

    def __init__(self, path: Path, callback: Callable[[], None]):
        self.path = path
        self.callback = callback
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="codex-global-state-watcher",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        if not hasattr(select, "kqueue"):
            return
        kernel_queue = select.kqueue()
        descriptor = None
        fingerprint = None
        try:
            try:
                details = self.path.stat()
                fingerprint = (
                    details.st_ino,
                    details.st_size,
                    details.st_mtime_ns,
                )
            except OSError:
                pass
            descriptor = os.open(
                self.path.parent,
                getattr(os, "O_EVTONLY", os.O_RDONLY),
            )
            event = select.kevent(
                descriptor,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                fflags=(
                    select.KQ_NOTE_WRITE
                    | select.KQ_NOTE_EXTEND
                    | select.KQ_NOTE_RENAME
                    | select.KQ_NOTE_DELETE
                ),
            )
            kernel_queue.control([event], 0, 0)
            while not self._stopped.is_set():
                events = kernel_queue.control(None, 8, 1)
                if events and not self._stopped.is_set():
                    try:
                        details = self.path.stat()
                        current = (
                            details.st_ino,
                            details.st_size,
                            details.st_mtime_ns,
                        )
                    except OSError:
                        current = None
                    if current != fingerprint:
                        fingerprint = current
                        self.callback()
        finally:
            kernel_queue.close()
            if descriptor is not None:
                os.close(descriptor)

    def close(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=2)


class RemoteHostMonitor:
    """Stream a remote Codex host over one reconnecting SSH process."""

    def __init__(
        self,
        host_id: str,
        alias: str,
        callback: Callable[[str, dict], None],
    ):
        self.host_id = host_id
        self.alias = alias
        self.callback = callback
        self._stopped = threading.Event()
        self._process: subprocess.Popen | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"codex-remote-{alias}",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

    def _run(self) -> None:
        retry = 0
        command = "python3 -u -c " + shlex.quote(REMOTE_WATCH_SCRIPT)
        while not self._stopped.is_set():
            try:
                self._process = subprocess.Popen(
                    [
                        "/usr/bin/ssh",
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ServerAliveInterval=15",
                        "-o",
                        "ServerAliveCountMax=12",
                        self.alias,
                        command,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
                assert self._process.stdout is not None
                for raw in self._process.stdout:
                    if self._stopped.is_set():
                        break
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    self.callback(self.host_id, event)
                    retry = 0
                if not self._stopped.is_set():
                    self.callback(self.host_id, {"type": "offline"})
            except Exception as error:
                self.callback(
                    self.host_id,
                    {"type": "offline", "message": str(error)},
                )
            finally:
                process = self._process
                self._process = None
                if process is not None:
                    self._terminate(process)
            retry += 1
            self._stopped.wait(min(15.0, 0.5 * (2 ** min(retry, 5))))

    def close(self) -> None:
        self._stopped.set()
        process = self._process
        if process is not None:
            self._terminate(process)
        self._thread.join(timeout=2)


def title_of(thread: dict) -> str:
    title = (
        thread.get("name")
        or thread.get("title")
        or thread.get("preview")
        or thread.get("firstUserMessage")
        or "Untitled"
    )
    return " ".join(str(title).split())[:72]


def usage_windows(response: dict | None) -> list[dict]:
    snapshot = (response or {}).get("rateLimits") or {}
    windows = []
    for value in (snapshot.get("primary"), snapshot.get("secondary")):
        if not value or value.get("usedPercent") is None:
            continue
        duration = value.get("windowDurationMins")
        kind = "five-hour" if duration is not None and duration <= 600 else "weekly"
        windows.append(
            {
                "kind": kind,
                "remainingPercent": max(
                    0, min(100, 100 - int(value["usedPercent"]))
                ),
                "resetsAt": value.get("resetsAt"),
            }
        )
    return windows


class NativeCodex:
    """Local state source; remote SSH monitoring is an explicit diagnostic."""

    def __init__(self, start: bool = True, enable_remote: bool = False):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._revision = 0
        self._stopped = threading.Event()
        self._enable_remote = enable_remote
        self._events: queue.Queue[tuple] = queue.Queue()
        self._client: AppServerClient | None = None
        self._native_navigation_notice_shown = False
        self._watcher: RolloutWatcher | None = None
        self._global_state_watcher: GlobalStateWatcher | None = None
        self._remote_monitors: dict[str, RemoteHostMonitor] = {}
        self._remote_online: dict[str, bool] = {}
        self._remote_errors: dict[str, str] = {}
        self._thread_assignments: dict[str, dict] = {}
        self._local_threads: list[dict] = []
        self._remote_threads: dict[str, list[dict]] = {}
        self._threads: list[dict] = []
        self._all_threads_by_path: dict[str, dict] = {}
        self._provisional_threads: dict[str, dict] = {}
        self._ignored_rollouts: set[str] = set()
        self._pending_rollout_directories: set[Path] = set()
        self._tails: dict[str, RolloutTail] = {}
        self._seen = self._load_seen()
        self._first_run = not bool(self._seen)
        self._usage: dict = {"windows": []}
        self._last_usage_refresh = 0.0
        self._state = {
            "connected": False,
            "source": "native",
            "slots": [],
            "usage": self._usage,
            "error": "Starting Codex app-server",
            "updatedAt": int(time.time() * 1000),
        }
        self._monitor: threading.Thread | None = None
        if start:
            self._monitor = threading.Thread(
                target=self._run,
                name="codex-native-monitor",
                daemon=True,
            )
            self._monitor.start()

    def _inventory_signature(self) -> tuple[str, ...]:
        return tuple(
            f"{thread.get('hostId') or 'local'}:{thread.get('id') or ''}"
            for thread in self._threads
        )

    @staticmethod
    def _tail_key(thread: dict) -> str | None:
        path = thread.get("path")
        if not path:
            return None
        host_id = str(thread.get("hostId") or "local")
        if host_id == "local":
            return str(Path(path).resolve())
        return f"{host_id}:{path}"

    @staticmethod
    def _activity_at(thread: dict) -> int:
        for key in ("recencyAt", "recency_at_ms", "updatedAt", "updated_at"):
            value = thread.get(key)
            if isinstance(value, (int, float)):
                value = int(value)
                return value * 1000 if value < 1_000_000_000_000 else value
            if isinstance(value, str) and value:
                try:
                    return int(datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    ).timestamp() * 1000)
                except ValueError:
                    continue
        return 0

    def _merge_threads(self) -> None:
        assigned_remote = {
            thread_id
            for thread_id, assignment in self._thread_assignments.items()
            if isinstance(assignment, dict)
            and assignment.get("projectKind") == "remote"
        }
        combined = [
            thread
            for thread in self._local_threads
            if str(thread.get("id") or "") not in assigned_remote
        ]
        for threads in self._remote_threads.values():
            combined.extend(threads)
        selected: dict[str, dict] = {}
        for thread in combined:
            thread_id = str(thread.get("id") or "")
            host_id = str(thread.get("hostId") or "local")
            key = f"{host_id}:{thread_id}"
            previous = selected.get(key)
            if previous is None or self._activity_at(thread) > self._activity_at(
                previous
            ):
                selected[key] = thread
        self._threads = sorted(
            selected.values(),
            key=self._activity_at,
            reverse=True,
        )[:SLOT_COUNT]

    def _sync_remote_hosts(self) -> bool:
        if not self._enable_remote:
            changed = bool(
                self._remote_monitors
                or self._remote_threads
            )
            assignments = thread_assignments()
            changed = assignments != self._thread_assignments or changed
            self._thread_assignments = assignments
            for monitor in tuple(self._remote_monitors.values()):
                monitor.close()
            self._remote_monitors.clear()
            self._remote_online.clear()
            self._remote_errors.clear()
            self._remote_threads.clear()
            self._merge_threads()
            return changed
        assignments = thread_assignments()
        hosts = remote_hosts_from_state()
        changed = assignments != self._thread_assignments
        self._thread_assignments = assignments
        for host_id in tuple(self._remote_monitors):
            if host_id in hosts:
                continue
            self._remote_monitors.pop(host_id).close()
            self._remote_online.pop(host_id, None)
            self._remote_errors.pop(host_id, None)
            self._remote_threads.pop(host_id, None)
            for key, thread in tuple(self._all_threads_by_path.items()):
                if thread.get("hostId") == host_id:
                    self._all_threads_by_path.pop(key, None)
                    self._tails.pop(key, None)
            changed = True
        for host_id, alias in hosts.items():
            monitor = self._remote_monitors.get(host_id)
            if monitor is not None and monitor.alias == alias:
                continue
            if monitor is not None:
                monitor.close()
            self._remote_online.setdefault(host_id, False)
            self._remote_monitors[host_id] = RemoteHostMonitor(
                host_id,
                alias,
                lambda current_host, event: self._events.put(
                    ("remote", current_host, event, time.monotonic())
                ),
            )
            changed = True
        self._merge_threads()
        return changed

    @staticmethod
    def _rollout_metadata(path: Path) -> tuple[dict | None, bool]:
        """Read only the new rollout header, never a fork's copied history."""
        try:
            with path.open("rb") as source:
                raw = source.readline(1024 * 1024)
            record = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # A create notification can precede the first buffered write.
            return None, False
        if record.get("type") != "session_meta":
            return None, False
        payload = record.get("payload") or {}
        if payload.get("thread_source") != "user":
            return None, True
        match = ROLLOUT_NAME.match(path.name)
        thread_id = str(
            payload.get("id")
            or payload.get("session_id")
            or (match.group(1) if match else "")
        )
        if not thread_id:
            return None, False
        try:
            recency_at = path.stat().st_mtime_ns // 1_000_000
        except OSError:
            recency_at = 0
        return (
            {
                "id": thread_id,
                "hostId": "local",
                "name": "",
                "title": "",
                "preview": "",
                "path": str(path.resolve()),
                # A directory kqueue event can cause discovery to scan rollout
                # files that predate this process. Preserve their real file
                # activity time; assigning time.now() here incorrectly promotes
                # stale tasks after Codex or the daemon restarts.
                "recencyAt": recency_at,
                "status": {"type": "notLoaded"},
                "_provisional": True,
            },
            False,
        )

    def _discover_rollouts(self, directory: Path) -> bool:
        """Promote new user rollouts directly from a directory kqueue event."""
        if not self._local_threads:
            self._local_threads = [
                thread
                for thread in self._threads
                if thread.get("hostId", "local") == "local"
            ]
        try:
            candidates = sorted(
                {
                    *directory.glob("rollout-*.jsonl"),
                    *directory.glob("*/rollout-*.jsonl"),
                    *directory.glob("*/*/rollout-*.jsonl"),
                },
                key=lambda item: item.stat().st_mtime_ns,
            )
        except OSError:
            return False
        changed = False
        watch_changed = False
        for path in candidates:
            normalized = str(path.resolve())
            if (
                normalized in self._tails
                or normalized in self._ignored_rollouts
            ):
                continue
            thread, permanently_ignored = self._rollout_metadata(path)
            if thread is None:
                if permanently_ignored:
                    self._ignored_rollouts.add(normalized)
                continue
            thread_id = thread["id"]
            tail = RolloutTail(path, recent_bytes=RECENT_ROLLOUT_BYTES)
            tail.update()
            self._tails[normalized] = tail
            self._all_threads_by_path[normalized] = thread
            self._provisional_threads[thread_id] = thread
            before = self._inventory_signature()
            self._local_threads = [
                thread,
                *(
                    item
                    for item in self._local_threads
                    if item.get("id") != thread_id
                ),
            ]
            self._merge_threads()
            changed = self._inventory_signature() != before or changed
            watch_changed = True
            print(
                "Codex new user rollout discovered at "
                f"{time.strftime('%H:%M:%S')} ({path.name}).",
                flush=True,
            )
        if watch_changed and self._watcher is not None:
            self._watcher.configure(
                [Path(path) for path in self._tails]
            )
            for tail in self._tails.values():
                tail.update()
        return changed

    def _load_seen(self) -> dict[str, int]:
        try:
            return {
                str(key): int(value)
                for key, value in json.loads(SEEN_PATH.read_text()).items()
            }
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return {}

    def _save_seen(self) -> None:
        APP_ROOT.mkdir(parents=True, exist_ok=True)
        temporary = SEEN_PATH.with_name(
            f".{SEEN_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(self._seen, indent=2, sort_keys=True) + "\n"
            )
            temporary.replace(SEEN_PATH)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _on_notification(self, message: dict) -> None:
        if message.get("method") in APP_SERVER_NOTIFICATION_METHODS:
            self._events.put(("notification", message))

    def _on_rollout_event(
        self,
        path: Path,
        is_directory: bool,
        flags: int,
    ) -> None:
        self._events.put(
            (
                "filesystem",
                path,
                is_directory,
                flags,
                time.monotonic(),
            )
        )

    def _refresh_inventory(self) -> bool:
        """Reload recent order, then close the watcher reconfigure gap.

        Threads are ordered by recent activity, matching Codex Micro's default
        Most Recent mode. Background rollout watches let an older task promote
        itself when it becomes active again. The second ``update`` pass is
        deliberately after ``configure`` so a racing final write is observed
        either by kqueue or by this catch-up read.
        """
        assert self._client is not None
        response = self._client.request(
            "thread/list",
            {
                "archived": False,
                "limit": MAX_WATCHED_ROLLOUTS,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "useStateDbOnly": True,
            },
        )
        known_threads = response.get("data") or []
        for thread in known_threads:
            thread["hostId"] = "local"
        known_ids = {
            str(thread.get("id") or "")
            for thread in known_threads
        }
        for thread_id in tuple(self._provisional_threads):
            if thread_id in known_ids:
                self._provisional_threads.pop(thread_id, None)
        provisional = [
            thread
            for thread_id, thread in reversed(
                tuple(self._provisional_threads.items())
            )
            if thread_id not in known_ids
        ]
        merged_threads = [*provisional, *known_threads]
        self._local_threads = merged_threads
        self._merge_threads()
        active_paths: list[str] = []
        active_path_set: set[str] = set()
        lifecycle_changed = False
        self._all_threads_by_path = {
            key: thread
            for key, thread in self._all_threads_by_path.items()
            if thread.get("hostId", "local") != "local"
        }
        for index, thread in enumerate(merged_threads):
            path = thread.get("path")
            if not path:
                continue
            normalized = str(Path(path).resolve())
            active_paths.append(normalized)
            active_path_set.add(normalized)
            self._all_threads_by_path[normalized] = thread
            tail = self._tails.get(normalized)
            if tail is None:
                # A fork can copy tens of MB of history. The current lifecycle
                # is always near the append edge, so visible tasks read only a
                # bounded tail while background tasks begin at EOF.
                tail = RolloutTail(
                    normalized,
                    start_at_end=index >= SLOT_COUNT,
                    recent_bytes=(
                        RECENT_ROLLOUT_BYTES
                        if index < SLOT_COUNT
                        else None
                    ),
                )
                self._tails[normalized] = tail
            lifecycle_changed = tail.update() or lifecycle_changed
        self._tails = {
            path: tail
            for path, tail in self._tails.items()
            if path in active_path_set
            or (
                path in self._all_threads_by_path
                and self._all_threads_by_path[path].get("hostId", "local")
                != "local"
            )
        }
        if self._watcher is not None:
            self._watcher.configure(
                [Path(path) for path in active_paths]
            )
            for tail in self._tails.values():
                lifecycle_changed = tail.update() or lifecycle_changed
        if self._first_run:
            for thread in self._threads:
                key = self._tail_key(thread)
                tail = self._tails.get(key) if key else None
                if tail and tail.completed_at:
                    self._seen[thread["id"]] = tail.completed_at
            self._first_run = False
            self._save_seen()
        return lifecycle_changed

    def _refresh_rate_limits(self) -> None:
        assert self._client is not None
        response = self._client.request("account/rateLimits/read")
        self._usage = {
            "windows": usage_windows(response),
            "updatedAt": int(time.time() * 1000),
        }
        self._last_usage_refresh = time.monotonic()

    def _publish(self, error: str | None = None) -> bool:
        slots = []
        for index, thread in enumerate(self._threads):
            host_id = str(thread.get("hostId") or "local")
            key = self._tail_key(thread)
            tail = self._tails.get(key) if key else None
            started_at = tail.started_at if tail else 0
            completed_at = tail.completed_at if tail else 0
            error_at = tail.error_at if tail else 0
            runtime = thread.get("status") or {}
            runtime_type = runtime.get("type")
            active_flags = set(runtime.get("activeFlags") or [])
            if tail and tail.pending_explicit_input:
                status = "input"
            elif (
                host_id != "local"
                and tail
                and tail.pending_input
            ):
                # A remote host has no local app-server notification stream, so
                # its rollout remains the best available approval signal. Local
                # escalation-shaped tool calls may already be pre-authorized
                # and must not replace an actively running task with "input".
                status = "input"
            elif error_at and error_at >= completed_at and error_at >= started_at:
                status = "error"
            elif runtime_type == "systemError":
                status = "error"
            elif runtime_type == "active" and active_flags & {
                "waitingOnApproval",
                "waitingOnUserInput",
            }:
                status = "input"
            elif runtime_type == "active":
                status = "thinking"
            elif started_at > completed_at:
                # Rollout lifecycle is authoritative for Desktop tasks whose
                # app-server inventory can remain idle during an active turn.
                status = "thinking"
            elif completed_at > self._seen.get(thread["id"], 0):
                status = "complete"
            else:
                status = "idle"
            slots.append(
                {
                    "id": index,
                    "threadKey": thread["id"],
                    "title": title_of(thread),
                    "status": status,
                    "path": thread.get("path"),
                    "hostId": host_id,
                    "hostOnline": (
                        True
                        if host_id == "local"
                        else self._remote_online.get(host_id, False)
                    ),
                    "hostError": self._remote_errors.get(host_id),
                }
            )
        with self._lock:
            state = {
                "connected": error is None,
                "source": "native",
                "slots": slots,
                "usage": deepcopy(self._usage),
                "error": error,
                "updatedAt": int(time.time() * 1000),
            }
            compared_keys = (
                "connected",
                "source",
                "slots",
                "usage",
                "error",
            )
            if all(
                self._state.get(key) == state.get(key)
                for key in compared_keys
            ):
                return False
            changed_fields = [
                key
                for key in compared_keys
                if self._state.get(key) != state.get(key)
            ]
            self._state = state
            self._revision += 1
            revision = self._revision
            self._changed.notify_all()
        summary = ", ".join(
            f"{slot['threadKey'][:8]}:{slot['status']}"
            for slot in slots
        )
        usage_summary = "/".join(
            f"{item.get('kind')}={item.get('remainingPercent')}"
            for item in self._usage.get("windows", [])
        ) or "unknown"
        print(
            f"Codex logical frame {revision} committed at "
            f"{time.strftime('%H:%M:%S')} [{summary}] "
            f"usage[{usage_summary}] changed[{','.join(changed_fields)}].",
            flush=True,
        )
        return True

    def _update_thread_status(self, thread_id: str, status: dict) -> bool:
        for thread in self._local_threads or self._threads:
            if (
                thread.get("id") == thread_id
                and thread.get("hostId", "local") == "local"
            ):
                if thread.get("status") == status:
                    return False
                thread["status"] = status
                return True
        return False

    def _handle_notification(self, message: dict) -> tuple[bool, bool]:
        """Return ``(state_changed, inventory_changed)`` for one notification."""
        method = message.get("method")
        params = message.get("params") or {}
        if method == "$/closed":
            raise RuntimeError("Codex app-server exited unexpectedly")
        if method == "account/rateLimits/updated":
            # Rolling updates are sparse. Refetch the complete snapshot rather
            # than accidentally clearing a window that the notification omitted.
            # app-server also emits a startup notification around the explicit
            # initial read. Treat that burst as one framebuffer, not two Usage
            # uploads a few hundred milliseconds apart.
            if (
                time.monotonic() - self._last_usage_refresh
                < STARTUP_USAGE_QUIET_SECONDS
            ):
                return False, False
            self._refresh_rate_limits()
            return True, False
        if method == "thread/status/changed":
            return (
                self._update_thread_status(
                    str(params.get("threadId") or ""),
                    params.get("status") or {},
                ),
                False,
            )
        if method == "turn/started":
            thread_id = str(params.get("threadId") or "")
            known = any(
                thread.get("id") == thread_id
                for thread in self._local_threads or self._threads
            )
            changed = self._update_thread_status(
                thread_id,
                {"type": "active", "activeFlags": []},
            )
            return changed or not known, not known
        if method == "turn/completed":
            thread_id = str(params.get("threadId") or "")
            known = any(
                thread.get("id") == thread_id
                for thread in self._local_threads or self._threads
            )
            turn_status = (params.get("turn") or {}).get("status")
            status = (
                {"type": "systemError"}
                if turn_status == "failed"
                else {"type": "idle"}
            )
            changed = self._update_thread_status(thread_id, status)
            return changed or not known, not known
        if method == "thread/name/updated":
            thread_id = str(params.get("threadId") or "")
            for thread in self._local_threads or self._threads:
                if thread.get("id") == thread_id:
                    thread["name"] = params.get("threadName")
                    return True, False
            return False, False
        if method == "thread/started":
            return True, True
        if method in {
            "thread/archived",
            "thread/deleted",
            "thread/unarchived",
        }:
            return True, True
        return False, False

    def _handle_filesystem(
        self,
        path: Path,
        is_directory: bool,
        flags: int,
    ) -> tuple[bool, bool]:
        structural = flags & (
            select.KQ_NOTE_DELETE
            | select.KQ_NOTE_RENAME
            | select.KQ_NOTE_REVOKE
        )
        if is_directory:
            self._pending_rollout_directories.add(path.resolve())
            return self._discover_rollouts(path), True
        tail = self._tails.get(str(path.resolve()))
        if tail is None:
            return False, bool(structural)
        started_at = tail.started_at
        lifecycle_changed = tail.update()
        # Starting an existing thread changes the Most Recent order unless it
        # is already at the front.
        started = tail.started_at > started_at
        first_path = self._tail_key(self._threads[0]) if self._threads else None
        needs_reorder = started and first_path != str(path.resolve())
        if needs_reorder:
            thread = self._all_threads_by_path.get(str(path.resolve()))
            if thread is not None:
                thread_id = thread.get("id")
                thread["recencyAt"] = int(time.time() * 1000)
                self._local_threads = [
                    thread,
                    *(
                        item
                        for item in self._local_threads
                        if item.get("id") != thread_id
                    ),
                ]
                self._merge_threads()
        return lifecycle_changed, needs_reorder or bool(structural)

    def _handle_remote(self, host_id: str, event: dict) -> bool:
        """Apply one event from a host's persistent SSH stream."""
        event_type = event.get("type")
        if event_type == "offline":
            changed = self._remote_online.get(host_id, False)
            self._remote_online[host_id] = False
            return changed
        was_online = self._remote_online.get(host_id, False)
        self._remote_online[host_id] = True
        if event_type == "warning":
            message = str(event.get("message") or "Remote state warning")
            previous = self._remote_errors.get(host_id)
            self._remote_errors[host_id] = message
            if not was_online or previous != message:
                print(
                    f"Codex remote {host_id} degraded to "
                    f"{event.get('mode') or 'limited'} state "
                    f"(SQLite {event.get('sqliteVersion') or 'unknown'}): {message}",
                    file=sys.stderr,
                    flush=True,
                )
            return not was_online or previous != message
        if event_type == "online":
            return not was_online
        if event_type == "inventory":
            self._remote_errors.pop(host_id, None)
            before = self._inventory_signature()
            threads = []
            active_keys = set()
            for value in event.get("threads") or []:
                if not isinstance(value, dict) or not value.get("id"):
                    continue
                thread = dict(value)
                thread["hostId"] = host_id
                threads.append(thread)
                key = self._tail_key(thread)
                if key is None:
                    continue
                active_keys.add(key)
                self._all_threads_by_path[key] = thread
                self._tails.setdefault(key, RolloutTail.streamed(thread["path"]))
            known_ids = {str(thread.get("id") or "") for thread in threads}
            provisional = [
                thread
                for thread in self._remote_threads.get(host_id, [])
                if thread.get("_provisional")
                and str(thread.get("id") or "") not in known_ids
            ]
            threads = [*provisional, *threads]
            for thread in provisional:
                key = self._tail_key(thread)
                if key is not None:
                    active_keys.add(key)
            for key, thread in tuple(self._all_threads_by_path.items()):
                if (
                    thread.get("hostId") == host_id
                    and key not in active_keys
                ):
                    self._all_threads_by_path.pop(key, None)
                    self._tails.pop(key, None)
            self._remote_threads[host_id] = threads
            self._merge_threads()
            return (
                not was_online
                or self._inventory_signature() != before
            )
        if event_type != "rollout":
            return not was_online
        path = event.get("path")
        encoded = event.get("data")
        if not isinstance(path, str) or not isinstance(encoded, str):
            return not was_online
        key = f"{host_id}:{path}"
        tail = self._tails.setdefault(key, RolloutTail.streamed(path))
        started_at = tail.started_at
        try:
            chunk = base64.b64decode(encoded, validate=True)
            lifecycle_changed = tail.feed(
                chunk,
                reset=bool(event.get("reset")),
            )
        except (ValueError, base64.binascii.Error):
            return not was_online
        # The first reset chunk is reconciliation, not new activity. Inventory
        # already carries its persisted recency; promoting it here would move
        # every stale remote task to the front after daemon/SSH reconnect.
        started = tail.started_at > started_at and not bool(event.get("reset"))
        thread = self._all_threads_by_path.get(key)
        if thread is None:
            for raw in chunk.splitlines():
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload") or {}
                if payload.get("thread_source") != "user":
                    break
                match = ROLLOUT_NAME.match(Path(path).name)
                thread_id = str(
                    payload.get("id")
                    or payload.get("session_id")
                    or (match.group(1) if match else "")
                )
                if not thread_id:
                    break
                thread = {
                    "id": thread_id,
                    "hostId": host_id,
                    "name": "",
                    "title": "",
                    "preview": "",
                    "path": path,
                    "recencyAt": int(time.time() * 1000),
                    "status": {"type": "notLoaded"},
                    "_provisional": True,
                }
                self._all_threads_by_path[key] = thread
                self._remote_threads[host_id] = [
                    thread,
                    *self._remote_threads.get(host_id, []),
                ]
                self._merge_threads()
                break
        if started and thread is not None:
            thread["recencyAt"] = int(time.time() * 1000)
            threads = self._remote_threads.get(host_id, [])
            self._remote_threads[host_id] = [
                thread,
                *(
                    item
                    for item in threads
                    if item.get("id") != thread.get("id")
                ),
            ]
            self._merge_threads()
            return True
        visible = any(self._tail_key(item) == key for item in self._threads)
        return not was_online or (lifecycle_changed and visible)

    def _run(self) -> None:
        self._sync_remote_hosts()
        self._global_state_watcher = GlobalStateWatcher(
            GLOBAL_STATE_PATH,
            lambda: self._events.put(("global_state", time.monotonic())),
        )
        retry = 0
        while not self._stopped.is_set():
            try:
                self._client = AppServerClient(find_codex_binary())
                self._client.on_notification(self._on_notification)
                self._watcher = RolloutWatcher(self._on_rollout_event)
                self._refresh_inventory()
                self._refresh_rate_limits()
                self._publish()
                retry = 0
                next_usage = time.monotonic() + USAGE_SECONDS
                inventory_due: float | None = None
                inventory_deadline: float | None = None
                inventory_baseline: tuple[str, ...] = ()
                inventory_pending_changed = False
                inventory_attempt = 0
                while not self._stopped.is_set():
                    now = time.monotonic()
                    if now >= next_usage:
                        self._refresh_rate_limits()
                        next_usage = time.monotonic() + USAGE_SECONDS
                        self._publish()
                        continue
                    if inventory_due is not None and now >= inventory_due:
                        for directory in tuple(
                            self._pending_rollout_directories
                        ):
                            self._discover_rollouts(directory)
                        # A newly discovered/promoted rollout already contains
                        # enough information for the visible five-key frame.
                        # Commit it after one short quiet window without waiting
                        # for the state DB or emitting an intermediate frame.
                        if self._inventory_signature() != inventory_baseline:
                            self._publish()
                            inventory_due = None
                            inventory_deadline = None
                            inventory_pending_changed = False
                            inventory_attempt = 0
                            self._pending_rollout_directories.clear()
                            continue

                        inventory_pending_changed = (
                            self._refresh_inventory()
                            or inventory_pending_changed
                        )
                        if (
                            self._inventory_signature() != inventory_baseline
                            or (
                                inventory_deadline is not None
                                and time.monotonic() >= inventory_deadline
                            )
                        ):
                            if (
                                self._inventory_signature()
                                != inventory_baseline
                                or inventory_pending_changed
                            ):
                                self._publish()
                            inventory_due = None
                            inventory_deadline = None
                            inventory_pending_changed = False
                            inventory_attempt = 0
                            self._pending_rollout_directories.clear()
                        else:
                            inventory_attempt += 1
                            inventory_due = (
                                time.monotonic()
                                + min(
                                    0.5,
                                    INVENTORY_DEBOUNCE_SECONDS
                                    * (2**inventory_attempt),
                                )
                            )
                        continue
                    deadlines = [next_usage]
                    if inventory_due is not None:
                        deadlines.append(inventory_due)
                    timeout = max(0.0, min(deadlines) - now)
                    try:
                        event = self._events.get(timeout=timeout)
                    except queue.Empty:
                        continue
                    batch = [event]
                    while True:
                        try:
                            batch.append(self._events.get_nowait())
                        except queue.Empty:
                            break
                    batch_inventory_before = self._inventory_signature()
                    changed = False
                    inventory_changed = False
                    for item in batch:
                        if item[0] == "stop":
                            return
                        if item[0] == "notification":
                            item_changed, item_inventory = (
                                self._handle_notification(item[1])
                            )
                            if (
                                item[1].get("method")
                                == "account/rateLimits/updated"
                            ):
                                next_usage = time.monotonic() + USAGE_SECONDS
                        elif item[0] == "filesystem":
                            item_changed, item_inventory = (
                                self._handle_filesystem(
                                    item[1],
                                    item[2],
                                    item[3],
                                )
                            )
                            if item_changed:
                                print(
                                    "Codex rollout state detected at "
                                    f"{time.strftime('%H:%M:%S')} "
                                    f"({(time.monotonic() - item[4]) * 1000:.1f}ms "
                                    "after kqueue callback).",
                                    flush=True,
                                )
                        elif item[0] == "remote":
                            item_changed = self._handle_remote(
                                item[1],
                                item[2],
                            )
                            item_inventory = False
                            if item_changed:
                                print(
                                    "Codex remote event detected at "
                                    f"{time.strftime('%H:%M:%S')} "
                                    f"({(time.monotonic() - item[3]) * 1000:.1f}ms "
                                    f"after SSH stream callback, {item[1]}).",
                                    flush=True,
                                )
                        elif item[0] == "global_state":
                            item_changed = self._sync_remote_hosts()
                            item_inventory = False
                        else:
                            continue
                        changed = changed or item_changed
                        inventory_changed = inventory_changed or item_inventory
                    if inventory_changed:
                        # Stage every structural/recent event into one logical
                        # framebuffer. Bursts extend the 50ms quiet window but
                        # never extend the two-second hard deadline.
                        if inventory_due is None:
                            inventory_baseline = batch_inventory_before
                            inventory_deadline = time.monotonic() + 2.0
                            inventory_attempt = 0
                        inventory_pending_changed = (
                            inventory_pending_changed or changed
                        )
                        inventory_due = min(
                            time.monotonic() + NEW_ROLLOUT_QUIET_SECONDS,
                            inventory_deadline
                            if inventory_deadline is not None
                            else float("inf"),
                        )
                    elif changed and inventory_due is None:
                        self._publish()
                    elif changed:
                        inventory_pending_changed = True
                    if time.monotonic() >= next_usage:
                        self._refresh_rate_limits()
                        next_usage = time.monotonic() + USAGE_SECONDS
                        self._publish()
            except Exception as error:
                self._publish(str(error))
                retry += 1
                self._stopped.wait(min(10.0, 0.5 * retry))
            finally:
                if self._watcher is not None:
                    self._watcher.close()
                    self._watcher = None
                if self._client is not None:
                    self._client.close()
                    self._client = None

    def snapshot(self, _force: bool = False) -> dict:
        with self._lock:
            return deepcopy(self._state)

    def wait_for_change(
        self,
        revision: int,
        timeout: float | None = None,
    ) -> tuple[int, dict]:
        with self._changed:
            self._changed.wait_for(
                lambda: self._revision != revision or self._stopped.is_set(),
                timeout=timeout,
            )
            return self._revision, deepcopy(self._state)

    def open_slot(self, index: int) -> None:
        state = self.snapshot()
        slots = state.get("slots") or []
        if index < 0 or index >= len(slots):
            raise IndexError(f"Native Codex slot {index} is not assigned")
        slot = slots[index]
        self.open_thread(
            str(slot["threadKey"]),
            host_id=str(slot.get("hostId") or "local"),
            title=str(slot.get("title") or ""),
        )

    def open_thread(
        self,
        thread_id: str,
        host_id: str | None = None,
        title: str = "",
    ) -> None:
        if host_id is None:
            candidates = [
                *self._threads,
                *self._local_threads,
                *(
                    thread
                    for threads in self._remote_threads.values()
                    for thread in threads
                ),
            ]
            routed = next(
                (
                    thread
                    for thread in candidates
                    if thread.get("id") == thread_id
                ),
                None,
            )
            host_id = (
                str(routed.get("hostId") or "local")
                if routed is not None
                else "local"
            )
            if not title and routed is not None:
                title = title_of(routed)
        slot_index = next(
            (
                index
                for index, slot in enumerate(
                    self.snapshot().get("slots") or []
                )
                if str(slot.get("threadKey") or "") == thread_id
                and str(slot.get("hostId") or "local") == host_id
            ),
            0,
        )
        if dispatch_bridge_thread(thread_id, slot_index):
            self._mark_thread_opened(thread_id, host_id)
            return
        normalized = str(thread_id).removeprefix("local:")
        if normalized.startswith("client-new-thread:"):
            raise RuntimeError(
                "Temporary Codex task requires an active Codex Bridge"
            )
        if host_id == "local":
            subprocess.Popen(
                [
                    "/usr/bin/open",
                    f"codex://threads/{quote(normalized, safe='')}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            open_remote_thread_from_dock(title)
        if not self._native_navigation_notice_shown:
            self._native_navigation_notice_shown = True
            show_native_navigation_notice()
        self._mark_thread_opened(thread_id, host_id)

    def _mark_thread_opened(self, thread_id: str, host_id: str) -> None:
        for thread in self._threads:
            if (
                thread.get("id") != thread_id
                or thread.get("hostId", "local") != host_id
            ):
                continue
            key = self._tail_key(thread)
            tail = self._tails.get(key) if key else None
            if tail and tail.completed_at:
                self._seen[thread_id] = max(
                    int(time.time() * 1000), tail.completed_at
                )
                self._save_seen()
                self._publish()
            break

    def mark_all_read(self) -> None:
        for thread in self._threads:
            key = self._tail_key(thread)
            tail = self._tails.get(key) if key else None
            if tail and tail.completed_at:
                self._seen[thread["id"]] = max(
                    int(time.time() * 1000), tail.completed_at
                )
        self._save_seen()
        self._publish()

    def desktop_action(self, action: str, pressed: bool = True) -> None:
        if action == "mark-read":
            if pressed:
                self.mark_all_read()
            return
        if action in BRIDGE_ACTIONS:
            if dispatch_bridge_action(action, pressed):
                return
            if not pressed:
                return
        if action == "steer":
            # A shortcut fallback can submit or queue the draft instead of
            # steering. The bridge owns this action so failure stays a no-op.
            return
        dispatch_desktop_action(action)

    def close(self) -> None:
        self._stopped.set()
        self._events.put(("stop",))
        with self._changed:
            self._changed.notify_all()
        if self._watcher is not None:
            self._watcher.close()
        if self._global_state_watcher is not None:
            self._global_state_watcher.close()
        for monitor in tuple(self._remote_monitors.values()):
            monitor.close()
        self._remote_monitors.clear()
        if self._client is not None:
            self._client.close()
        if self._monitor is not None:
            self._monitor.join(timeout=2)


def fetch_bridge_state(
    bridge_url: str = BRIDGE_URL,
    timeout: float = 0.6,
) -> dict:
    """Read the sidecar's already-refreshed renderer cache."""
    request = Request(f"{bridge_url}/state", method="GET")
    with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("Codex bridge returned a non-object state")
    return payload


class CodexStateAdapter:
    """Prefer renderer Micro state and fall back to local-only NativeCodex."""

    POLL_SECONDS = 0.250
    FALLBACK_AFTER_FAILURES = 3

    def __init__(
        self,
        start: bool = True,
        bridge_url: str = BRIDGE_URL,
    ):
        self._bridge_url = bridge_url
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._revision = 0
        self._stopped = threading.Event()
        self._fallback: NativeCodex | None = None
        self._bridge_failures = 0
        self._announced_source: str | None = None
        self._state = {
            "connected": False,
            "source": "bridge",
            "slots": [],
            "usage": {"windows": []},
            "error": "Waiting for Codex Bridge",
            "updatedAt": int(time.time() * 1000),
        }
        self._monitor: threading.Thread | None = None
        if start:
            self._monitor = threading.Thread(
                target=self._run,
                name="codex-state-adapter",
                daemon=True,
            )
            self._monitor.start()

    @staticmethod
    def _bridge_frame(payload: dict) -> dict:
        slots = []
        for index, raw in enumerate((payload.get("slots") or [])[:SLOT_COUNT]):
            if not isinstance(raw, dict) or not raw.get("threadKey"):
                continue
            slots.append({
                "id": index,
                "threadKey": str(raw["threadKey"]),
                "title": str(raw.get("title") or "Untitled"),
                "status": str(raw.get("status") or "idle"),
                "hostId": str(raw.get("hostId") or "renderer"),
                "selected": bool(raw.get("selected")),
            })
        usage = payload.get("usage")
        return {
            "connected": True,
            "source": "bridge",
            "slots": slots,
            "usage": usage if isinstance(usage, dict) else {"windows": []},
            "error": None,
            "updatedAt": int(payload.get("updatedAt") or time.time() * 1000),
        }

    def _publish(self, state: dict) -> bool:
        compared = ("connected", "source", "slots", "usage", "error")
        with self._changed:
            if all(self._state.get(key) == state.get(key) for key in compared):
                return False
            self._state = deepcopy(state)
            self._revision += 1
            self._changed.notify_all()
        source = str(state.get("source") or "unknown")
        if self._announced_source != source:
            print(
                f"Codex state source switched to {source}.",
                flush=True,
            )
            self._announced_source = source
        return True

    def _close_fallback(self) -> None:
        fallback = self._fallback
        self._fallback = None
        if fallback is not None:
            fallback.close()

    def _poll_once(self) -> None:
        error = None
        try:
            payload = fetch_bridge_state(self._bridge_url)
            if not payload.get("connected"):
                raise RuntimeError(str(payload.get("error") or "Bridge disconnected"))
            self._bridge_failures = 0
            self._publish(self._bridge_frame(payload))
            self._close_fallback()
            return
        except (
            HTTPError,
            URLError,
            TimeoutError,
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as caught:
            error = caught
            self._bridge_failures += 1

        with self._lock:
            bridge_is_active = self._state.get("source") == "bridge"
        if bridge_is_active and self._bridge_failures < self.FALLBACK_AFTER_FAILURES:
            return
        if self._fallback is None:
            self._fallback = NativeCodex(enable_remote=False)
        state = self._fallback.snapshot()
        state["source"] = "native-local"
        if not state.get("connected") and not state.get("error"):
            state["error"] = str(error or "Codex Bridge unavailable")
        self._publish(state)

    def _run(self) -> None:
        while not self._stopped.is_set():
            self._poll_once()
            self._stopped.wait(self.POLL_SECONDS)

    def snapshot(self, _force: bool = False) -> dict:
        with self._lock:
            return deepcopy(self._state)

    def wait_for_change(
        self,
        revision: int,
        timeout: float | None = None,
    ) -> tuple[int, dict]:
        with self._changed:
            self._changed.wait_for(
                lambda: self._revision != revision or self._stopped.is_set(),
                timeout=timeout,
            )
            return self._revision, deepcopy(self._state)

    def open_thread(
        self,
        thread_id: str,
        host_id: str | None = None,
        title: str = "",
    ) -> None:
        slot_index = next(
            (
                index
                for index, slot in enumerate(self.snapshot().get("slots") or [])
                if str(slot.get("threadKey") or "") == str(thread_id)
            ),
            0,
        )
        if dispatch_bridge_thread(
            thread_id,
            slot_index,
            bridge_url=self._bridge_url,
        ):
            return
        normalized = str(thread_id).removeprefix("local:")
        if re.fullmatch(THREAD_UUID_PATTERN, normalized):
            subprocess.Popen(
                ["/usr/bin/open", f"codex://threads/{quote(normalized, safe='')}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        print(
            f"Temporary Codex task cannot open while Bridge is unavailable: {normalized}",
            file=sys.stderr,
            flush=True,
        )

    def desktop_action(self, action: str, pressed: bool = True) -> None:
        with self._lock:
            bridge_mode = self._state.get("source") == "bridge"
        if action in BRIDGE_ACTIONS and bridge_mode:
            # Never replay a one-shot action through AppleScript after a bridge
            # error: New/Fork/Submit may already have executed before the HTTP
            # response was lost.
            dispatch_bridge_action(
                action,
                pressed,
                bridge_url=self._bridge_url,
            )
            return
        if not pressed:
            return
        if action == "steer":
            return
        dispatch_desktop_action(action)

    def close(self) -> None:
        self._stopped.set()
        with self._changed:
            self._changed.notify_all()
        if self._monitor is not None:
            self._monitor.join(timeout=2)
        self._close_fallback()
