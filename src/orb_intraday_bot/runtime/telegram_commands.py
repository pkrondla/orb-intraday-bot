"""Authorized Telegram command poller.

Long-polls getUpdates on a background thread. Only messages from TELEGRAM_CHAT_ID are accepted.
Commands are put on a thread-safe queue; the trading loop (the single writer) drains them.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

log = logging.getLogger("orb.telegram_cmd")
_TIMEOUT_SEC = 35          # long-poll timeout + a little slack
_POLL_TIMEOUT = 25


def normalize_chat_id(value: object) -> str:
    """Strip quotes/whitespace so GitHub vars and Telegram ids compare equal."""
    text = str(value or "").strip().strip('"').strip("'")
    try:
        return str(int(text))
    except (TypeError, ValueError):
        return text


def chats_match(left: object, right: object) -> bool:
    a, b = normalize_chat_id(left), normalize_chat_id(right)
    return bool(a) and a == b
_HELP = (
    "ORB bot commands:\n"
    "Status\n"
    "/status - alive? mode, risk, today's trading state\n"
    "/mode - mode and the settings behind it\n"
    "/version - version and rule fingerprints\n"
    "/risk - limits, kill switch, entry blocks\n"
    "/funds - capital and available margin\n"
    "/log [n] - last n log lines (default 15)\n"
    "Trading today\n"
    "/today - today's trades and P&L\n"
    "/pnl [day|week|month] - P&L over a period\n"
    "/positions - open positions\n"
    "/progress - stops, targets and broker stop per open position\n"
    "/signals - today's signals and what happened to each\n"
    "/orders - today's orders and fills\n"
    "/levels - today's pivots and opening range\n"
    "/report - forward-test statistics\n"
    "Control\n"
    "/stop - no new trades for the rest of today (exits still run); trading restarts tomorrow\n"
    "/start - allow new trades again today\n"
    "/exit <symbol> - close one open position now (a part of the symbol is enough)\n"
    "/flatten - close every open position now\n"
    "/rearm - put the broker-side stop back on open positions\n"
    "/reconcile - check the bot's books against the broker now\n"
    "/clearhalt - clear the kill switch / entry block so new entries are allowed\n"
    "/restart - restart the service (open positions and broker stops are kept)\n"
    "/limits - capital, risk per trade, daily loss limit and lot cap now in force\n"
    "/capital <amount> - capital the bot sizes against (e.g. 750000 or 7.5L)\n"
    "/riskpct <percent> - risk per trade as % of capital (e.g. 1.5)\n"
    "/dailyloss <percent> - daily loss limit as % of capital; trading halts past it\n"
    "/maxlots <n> - most lots in one trade (/limits reset restores the defaults)\n"
    "/dryrun [on|off] - show or switch DRY_RUN: off = REAL orders, on = no orders reach the "
    "broker (restarts the bot; only with no open position)\n"
    "/help - this list"
)


class CommandKind(StrEnum):
    STATUS = "status"
    TODAY = "today"
    POSITIONS = "positions"
    REPORT = "report"
    STOP_TODAY = "stop_today"
    START_TODAY = "start_today"
    RISK = "risk"
    ORDERS = "orders"
    SIGNALS = "signals"
    LEVELS = "levels"
    FUNDS = "funds"
    PNL = "pnl"
    PROGRESS = "progress"
    MODE = "mode"
    VERSION = "version"
    LOG = "log"
    EXIT = "exit"
    REARM = "rearm"
    RECONCILE = "reconcile"
    CLEARHALT = "clearhalt"
    RESTART = "restart"
    DRYRUN = "dryrun"
    LIMITS = "limits"
    CAPITAL = "capital"
    RISKPCT = "riskpct"
    DAILYLOSS = "dailyloss"
    MAXLOTS = "maxlots"
    FLATTEN = "flatten"
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
        "risk": CommandKind.RISK,
        "orders": CommandKind.ORDERS,
        "signals": CommandKind.SIGNALS,
        "levels": CommandKind.LEVELS,
        "funds": CommandKind.FUNDS,
        "pnl": CommandKind.PNL,
        "progress": CommandKind.PROGRESS,
        "mode": CommandKind.MODE,
        "version": CommandKind.VERSION,
        "log": CommandKind.LOG,
        "logs": CommandKind.LOG,
        "exit": CommandKind.EXIT,
        "rearm": CommandKind.REARM,
        "reconcile": CommandKind.RECONCILE,
        "clearhalt": CommandKind.CLEARHALT,
        "restart": CommandKind.RESTART,
        "dryrun": CommandKind.DRYRUN,
        "limits": CommandKind.LIMITS,
        "capital": CommandKind.CAPITAL,
        "riskpct": CommandKind.RISKPCT,
        "dailyloss": CommandKind.DAILYLOSS,
        "maxlots": CommandKind.MAXLOTS,
        "stop": CommandKind.STOP_TODAY,
        "pause": CommandKind.STOP_TODAY,
        "start": CommandKind.START_TODAY,
        "resume": CommandKind.START_TODAY,
        "flatten": CommandKind.FLATTEN,
        "help": CommandKind.HELP,
    }
    kind = mapping.get(verb, CommandKind.UNKNOWN)
    return Command(kind=kind, arg=arg)


class CommandBus:
    """Thread-safe queue of commands and the operator's stop-for-today flag."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Command] = queue.Queue()
        self._lock = threading.Lock()
        # The day (IST) on which the operator sent /stop. Trading is on by default every day and
        # a stop expires by itself: it only ever applies to the day it names.
        self.stopped_day: date | None = None

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

    def stop_for(self, day: date) -> None:
        with self._lock:
            self.stopped_day = day

    def start_for(self, day: date) -> None:
        with self._lock:
            if self.stopped_day == day:
                self.stopped_day = None

    def is_stopped(self, day: date) -> bool:
        """True if the operator stopped trading for `day`. Never carries over to another day."""
        with self._lock:
            return self.stopped_day == day


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
        self._token = token.strip()
        self._chat_id = normalize_chat_id(chat_id)
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
        self._delete_webhook()
        log.info("Telegram commands listening for chat %s", self._chat_id)
        self._thread = threading.Thread(
            target=self._run, name="orb-telegram-commands", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>") if self._token else text

    def _delete_webhook(self) -> None:
        """getUpdates is refused while a webhook is set (HTTP 409)."""
        url = f"https://api.telegram.org/bot{self._token}/deleteWebhook"
        try:
            raw = self._get(url)
            payload = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            log.warning("Telegram deleteWebhook failed: %s", type(exc).__name__)
            return
        if not payload.get("ok"):
            log.warning("Telegram deleteWebhook rejected: %s", payload.get("description", "unknown"))

    def _fetch(self, url: str) -> bytes | None:
        try:
            return self._get(url)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            detail = self._redact((body or exc.reason or "")[:240])
            log.warning("Telegram poll failed: HTTP %s %s", exc.code, detail)
            self._sleep(5.0 if exc.code == 409 else 2.0)
            return None
        except urllib.error.URLError:
            log.warning("Telegram poll failed: URLError")
            self._sleep(2.0)
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                log.warning("Telegram poll failed: %s", type(exc).__name__)
                self._sleep(2.0)

    def _poll_once(self) -> None:
        # Do not pass allowed_updates: a bad encoding can make Telegram drop messages
        # while sendMessage (startup alerts) still works.
        params = urllib.parse.urlencode({"timeout": _POLL_TIMEOUT, "offset": self._offset})
        url = f"https://api.telegram.org/bot{self._token}/getUpdates?{params}"
        raw = self._fetch(url)
        if raw is None:
            return
        try:
            payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not payload.get("ok"):
            log.warning(
                "Telegram getUpdates rejected: %s",
                self._redact(str(payload.get("description", "unknown"))),
            )
            return
        for update in payload.get("result") or []:
            self._handle_update(update)

    def _handle_update(self, update: dict[str, Any]) -> None:
        update_id = int(update.get("update_id", 0))
        if update_id >= self._offset:
            self._offset = update_id + 1
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = normalize_chat_id(chat.get("id", ""))
        sender_id = normalize_chat_id(sender.get("id", ""))
        if not chats_match(chat_id, self._chat_id) and not chats_match(sender_id, self._chat_id):
            if chat_id or sender_id:
                log.info(
                    "Ignoring Telegram message from unauthorized chat %s sender %s",
                    chat_id,
                    sender_id,
                )
            return
        text = str(message.get("text") or message.get("caption") or "")
        command = parse_command(text)
        command = Command(
            kind=command.kind, arg=command.arg, chat_id=chat_id or sender_id, update_id=update_id
        )
        log.info("Telegram command %s from chat %s", command.kind.value, command.chat_id)
        if command.kind is CommandKind.HELP:
            self._reply(_HELP)
            return
        if command.kind is CommandKind.UNKNOWN:
            self._reply("Unknown command. /help for the list.")
            return
        self._bus.put(command)


def build_command_poller(
    settings: Any,
    bus: CommandBus,
    reply: ReplyFunc,
) -> TelegramCommandPoller | None:
    """Return a poller when Telegram is configured, else None."""
    token = settings.telegram_bot_token.get_secret_value().strip()
    chat = normalize_chat_id(settings.telegram_chat_id)
    if not token or not chat:
        log.warning("Telegram commands off: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is empty")
        return None
    return TelegramCommandPoller(token, chat, bus, reply)
