"""Authorized Telegram command poller.

Long-polls getUpdates on a background thread. Only messages from TELEGRAM_CHAT_ID are accepted.
Commands are put on a thread-safe queue; the trading loop (the single writer) drains them.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

log = logging.getLogger("orb.telegram_cmd")
_TIMEOUT_SEC = 35          # long-poll timeout + a little slack
_POLL_TIMEOUT = 25
_HELP = (
    "ORB bot commands:\n"
    "/status - is the bot alive and what is it doing\n"
    "/today - today's trades and P&L\n"
    "/positions - open positions\n"
    "/report - forward-test statistics\n"
    "/pause - stop opening new trades (exits still run)\n"
    "/resume - allow new trades again\n"
    "/flatten - close everything (reply /flatten <code> to confirm)\n"
    "/arm <code> - arm live trading for today (when LIVE_MODE=live)\n"
    "/disarm - cancel today's arm\n"
    "/help - this list"
)


class CommandKind(StrEnum):
    STATUS = "status"
    TODAY = "today"
    POSITIONS = "positions"
    REPORT = "report"
    PAUSE = "pause"
    RESUME = "resume"
    FLATTEN = "flatten"
    FLATTEN_CONFIRM = "flatten_confirm"
    ARM = "arm"
    DISARM = "disarm"
    HELP = "help"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Command:
    kind: CommandKind
    arg: str = ""
    chat_id: str = ""
    update_id: int = 0


GetFunc = Callable[[str], bytes]
SleepFunc = Callable[[float], None]
ReplyFunc = Callable[[str], None]


def parse_command(text: str) -> Command:
    """Parse a message body into a Command. BotFather may append @botname to the verb."""
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return Command(CommandKind.UNKNOWN, arg=raw)
    parts = raw.split(maxsplit=1)
    verb = parts[0][1:].split("@", 1)[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    mapping = {
        "status": CommandKind.STATUS,
        "today": CommandKind.TODAY,
        "positions": CommandKind.POSITIONS,
        "report": CommandKind.REPORT,
        "pause": CommandKind.PAUSE,
        "resume": CommandKind.RESUME,
        "flatten": CommandKind.FLATTEN if not arg else CommandKind.FLATTEN_CONFIRM,
        "arm": CommandKind.ARM,
        "disarm": CommandKind.DISARM,
        "help": CommandKind.HELP,
        "start": CommandKind.HELP,
    }
    kind = mapping.get(verb, CommandKind.UNKNOWN)
    return Command(kind=kind, arg=arg)


class CommandBus:
    """Thread-safe queue of commands plus a pending flatten-confirm code."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Command] = queue.Queue()
        self._lock = threading.Lock()
        self._flatten_code: str | None = None
        self._arm_code: str | None = None
        self.paused = False
        self.armed_today = False

    def put(self, command: Command) -> None:
        self._queue.put(command)

    def drain(self, limit: int = 50) -> list[Command]:
        out: list[Command] = []
        for _ in range(limit):
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def begin_flatten(self) -> str:
        code = secrets.token_hex(2)
        with self._lock:
            self._flatten_code = code
        return code

    def confirm_flatten(self, code: str) -> bool:
        with self._lock:
            ok = bool(self._flatten_code) and code.strip().lower() == self._flatten_code
            if ok:
                self._flatten_code = None
            return ok

    def begin_arm(self) -> str:
        code = secrets.token_hex(2)
        with self._lock:
            self._arm_code = code
        return code

    def confirm_arm(self, code: str) -> bool:
        with self._lock:
            ok = bool(self._arm_code) and code.strip().lower() == self._arm_code
            if ok:
                self._arm_code = None
                self.armed_today = True
            return ok

    def disarm(self) -> None:
        with self._lock:
            self._arm_code = None
            self.armed_today = False


def _urllib_get(url: str) -> bytes:  # pragma: no cover - network
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:   # noqa: S310
        return bytes(response.read())


class TelegramCommandPoller:
    """Background long-poll of getUpdates. Drop-in for the serve loop."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        bus: CommandBus,
        reply: ReplyFunc,
        get: GetFunc = _urllib_get,
        sleep: SleepFunc = time.sleep,
    ) -> None:
        self._token = token
        self._chat_id = str(chat_id)
        self._bus = bus
        self._reply = reply
        self._get = get
        self._sleep = sleep
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="orb-telegram-commands", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                log.warning("Telegram poll failed: %s", type(exc).__name__)
                self._sleep(2.0)

    def _poll_once(self) -> None:
        params = urllib.parse.urlencode(
            {"timeout": _POLL_TIMEOUT, "offset": self._offset, "allowed_updates": '["message"]'}
        )
        url = f"https://api.telegram.org/bot{self._token}/getUpdates?{params}"
        try:
            raw = self._get(url)
        except urllib.error.URLError:
            self._sleep(2.0)
            return
        try:
            payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not payload.get("ok"):
            return
        for update in payload.get("result") or []:
            self._handle_update(update)

    def _handle_update(self, update: dict[str, Any]) -> None:
        update_id = int(update.get("update_id", 0))
        if update_id >= self._offset:
            self._offset = update_id + 1
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if chat_id != self._chat_id:
            if chat_id:
                log.info("Ignoring Telegram message from unauthorized chat %s", chat_id)
            return
        text = str(message.get("text") or "")
        command = parse_command(text)
        command = Command(
            kind=command.kind, arg=command.arg, chat_id=chat_id, update_id=update_id
        )
        if command.kind is CommandKind.HELP:
            self._reply(_HELP)
            return
        if command.kind is CommandKind.UNKNOWN:
            self._reply("Unknown command. /help for the list.")
            return
        if command.kind is CommandKind.FLATTEN:
            code = self._bus.begin_flatten()
            self._reply(
                f"Confirm flatten: reply /flatten {code} within a few minutes. "
                "This closes every open position this bot owns."
            )
            return
        self._bus.put(command)


def build_command_poller(
    settings: Any,
    bus: CommandBus,
    reply: ReplyFunc,
) -> TelegramCommandPoller | None:
    """Return a poller when Telegram is configured, else None."""
    token = settings.telegram_bot_token.get_secret_value()
    chat = settings.telegram_chat_id
    if not token or not chat:
        return None
    return TelegramCommandPoller(token, chat, bus, reply)
