"""Native Codex adapter used by the D200 daemon.

The adapter talks to the bundled Codex app-server for inventory and account
usage, watches rollout JSONL with macOS kernel events for the Desktop task
lifecycle, and uses the official ``codex://threads/<id>`` deep link for task
switching.  It never requires Chromium remote debugging or state polling.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import resource
import select
import subprocess
import threading
import time
import tomllib
from typing import Callable
from urllib.parse import quote


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
SLOT_COUNT = 5
MAX_WATCHED_ROLLOUTS = 2048
USAGE_SECONDS = 600.0
INVENTORY_DEBOUNCE_SECONDS = 0.150
NEW_ROLLOUT_QUIET_SECONDS = 0.050
STARTUP_USAGE_QUIET_SECONDS = 2.0
RECENT_ROLLOUT_BYTES = 2 * 1024 * 1024
ROLLOUT_NAME = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def desktop_settings(config_path: Path | None = None) -> dict:
    """Read the Desktop's own settings; missing values keep Codex defaults."""
    path = config_path or (Path.home() / ".codex" / "config.toml")
    try:
        config = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    desktop = config.get("desktop")
    return desktop if isinstance(desktop, dict) else {}


def follow_up_mode(config_path: Path | None = None) -> str:
    """Read Codex's own follow-up behavior instead of duplicating a setting."""
    desktop = desktop_settings(config_path)
    value = desktop.get(
        "followUpQueueMode",
        desktop.get("follow_up_queue_mode"),
    )
    if value == "interrupt":
        return "steer"
    return str(value) if value in {"queue", "steer"} else "steer"


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


def steer_key_script(config_path: Path | None = None) -> str:
    # Desktop derives the inverse shortcut from both settings. When plain
    # Enter sends, the inverse is Cmd+Enter; Cmd+Shift+Enter is only correct
    # for the two Cmd+Enter send modes.
    if follow_up_mode(config_path) == "queue":
        if composer_enter_behavior(config_path) == "enter":
            return "key code 36 using command down"
        return "key code 36 using {command down, shift down}"
    # Cmd+Enter always submits and follows the default Steer behavior.
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
        "steer": steer_key_script(),
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
                    "version": "0.2.0",
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
        if size == self.offset:
            return False
        with self.path.open("rb") as source:
            source.seek(self.offset)
            chunk = source.read()
            self.offset = source.tell()
        before = (
            self.started_at,
            self.completed_at,
            self.error_at,
            frozenset(self.pending_input),
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
                elif event_type == "turn_aborted":
                    self.completed_at = max(self.completed_at, timestamp)
                    self.pending_input.clear()
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
            elif (
                item_type in {
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_search_output",
                }
                and call_id
            ):
                self.pending_input.discard(call_id)
        return before != (
            self.started_at,
            self.completed_at,
            self.error_at,
            frozenset(self.pending_input),
        )


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
    """Thread-safe, event-driven state source for the D200 runtime."""

    def __init__(self, start: bool = True):
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._revision = 0
        self._stopped = threading.Event()
        self._events: queue.Queue[tuple] = queue.Queue()
        self._client: AppServerClient | None = None
        self._watcher: RolloutWatcher | None = None
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
        return tuple(str(thread.get("id") or "") for thread in self._threads)

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
        return (
            {
                "id": thread_id,
                "name": "",
                "title": "",
                "preview": "",
                "path": str(path.resolve()),
                "status": {"type": "notLoaded"},
                "_provisional": True,
            },
            False,
        )

    def _discover_rollouts(self, directory: Path) -> bool:
        """Promote new user rollouts directly from a directory kqueue event."""
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
            self._threads = [
                thread,
                *(
                    item
                    for item in self._threads
                    if item.get("id") != thread_id
                ),
            ][:SLOT_COUNT]
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
        self._threads = merged_threads[:SLOT_COUNT]
        active_paths: list[str] = []
        active_path_set: set[str] = set()
        lifecycle_changed = False
        self._all_threads_by_path = {}
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
        }
        if self._watcher is not None:
            self._watcher.configure(
                [Path(path) for path in active_paths]
            )
            for tail in self._tails.values():
                lifecycle_changed = tail.update() or lifecycle_changed
        if self._first_run:
            for thread in self._threads:
                path = thread.get("path")
                tail = self._tails.get(str(Path(path).resolve())) if path else None
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
            path = thread.get("path")
            tail = self._tails.get(str(Path(path).resolve())) if path else None
            started_at = tail.started_at if tail else 0
            completed_at = tail.completed_at if tail else 0
            error_at = tail.error_at if tail else 0
            runtime = thread.get("status") or {}
            runtime_type = runtime.get("type")
            active_flags = set(runtime.get("activeFlags") or [])
            if tail and tail.pending_input:
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
        for thread in self._threads:
            if thread.get("id") == thread_id:
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
                for thread in self._threads
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
                for thread in self._threads
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
            for thread in self._threads:
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
        first_path = (
            str(Path(self._threads[0]["path"]).resolve())
            if self._threads and self._threads[0].get("path")
            else None
        )
        needs_reorder = started and first_path != str(path.resolve())
        if needs_reorder:
            thread = self._all_threads_by_path.get(str(path.resolve()))
            if thread is not None:
                thread_id = thread.get("id")
                self._threads = [
                    thread,
                    *(
                        item
                        for item in self._threads
                        if item.get("id") != thread_id
                    ),
                ][:SLOT_COUNT]
        return lifecycle_changed, needs_reorder or bool(structural)

    def _run(self) -> None:
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
                        else:
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
        self.open_thread(slots[index]["threadKey"])

    def open_thread(self, thread_id: str) -> None:
        subprocess.Popen(
            ["/usr/bin/open", f"codex://threads/{quote(thread_id, safe='')}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for thread in self._threads:
            if thread.get("id") != thread_id:
                continue
            path = thread.get("path")
            tail = self._tails.get(str(Path(path).resolve())) if path else None
            if tail and tail.completed_at:
                self._seen[thread_id] = max(
                    int(time.time() * 1000), tail.completed_at
                )
                self._save_seen()
                self._publish()
            break

    def mark_all_read(self) -> None:
        for thread in self._threads:
            path = thread.get("path")
            tail = self._tails.get(str(Path(path).resolve())) if path else None
            if tail and tail.completed_at:
                self._seen[thread["id"]] = max(
                    int(time.time() * 1000), tail.completed_at
                )
        self._save_seen()
        self._publish()

    def desktop_action(self, action: str) -> None:
        if action == "mark-read":
            self.mark_all_read()
            return
        dispatch_desktop_action(action)

    def close(self) -> None:
        self._stopped.set()
        self._events.put(("stop",))
        with self._changed:
            self._changed.notify_all()
        if self._watcher is not None:
            self._watcher.close()
        if self._client is not None:
            self._client.close()
        if self._monitor is not None:
            self._monitor.join(timeout=2)
