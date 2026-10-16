"""Alerts. Optional Telegram, never able to stop or slow the bot.

Telegram is off unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set. A failed send is logged
and ignored. Sends go through a bounded outbox so the trading loop never waits on the network.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import suppress
from typing import Protocol

from orb_intraday_bot.config.settings import Settings

log = logging.getLogger("orb.notify")
_TIMEOUT_SEC = 10
_MAX_LEN = 3900          # Telegram's limit is 4096 characters per message
_OUTBOX_SIZE = 200
_RETRIES = 3
_BACKOFF_SEC = (0.5, 1.5, 3.0)


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class NullNotifier:
    def send(self, text: str) -> None:
        return None


PostFunc = Callable[[str, bytes], bytes]
SleepFunc = Callable[[float], None]


def _urllib_post(url: str, body: bytes) -> bytes:  # pragma: no cover - network
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:   # noqa: S310
        return bytes(response.read())


def split_message(text: str, max_len: int = _MAX_LEN) -> list[str]:
    """Split a long message on newlines when possible, else hard-cut."""
    if len(text) <= max_len:
        return [text] if text else []
    parts: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_len:
            parts.append(rest)
            break
        cut = rest.rfind("\n", 0, max_len + 1)
        if cut < max_len // 2:
            cut = max_len
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return parts


class TelegramNotifier:
    """Sends via the Bot API. Never raises into the caller.

    When `async_outbox` is True (the default for live use), `send` enqueues the text and a
    background thread delivers it with retries. When False, delivery is synchronous (tests).
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        post: PostFunc = _urllib_post,
        sleep: SleepFunc = time.sleep,
        *,
        async_outbox: bool = True,
    ) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._post = post
        self._sleep = sleep
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_OUTBOX_SIZE)
        self._thread: threading.Thread | None = None
        if async_outbox:
            self._thread = threading.Thread(
                target=self._worker, name="orb-telegram-outbox", daemon=True
            )
            self._thread.start()

    def send(self, text: str) -> None:
        for part in split_message(text):
            if self._thread is None:
                self._deliver(part)
                continue
            try:
                self._queue.put_nowait(part)
            except queue.Full:
                log.warning("Telegram outbox full; dropping a message")

    def close(self, timeout: float = 5.0) -> None:
        """Flush the outbox (best effort) and stop the worker. Safe to call more than once."""
        if self._thread is None:
            return
        with suppress(queue.Full):
            self._queue.put(None, timeout=timeout)
        self._thread.join(timeout=timeout)
        self._thread = None

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._deliver(item)

    def _deliver(self, text: str) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(_RETRIES):
            try:
                self._post(self._url, body)
                return
            except Exception as exc:        # never let an alert failure reach the trading loop
                last_err = exc
                if attempt < _RETRIES - 1:
                    self._sleep(_BACKOFF_SEC[min(attempt, len(_BACKOFF_SEC) - 1)])
        # The URL holds the bot token, so only the error type is logged.
        name = type(last_err).__name__ if last_err else "Unknown"
        detail = ""
        if isinstance(last_err, urllib.error.HTTPError):
            detail = f" HTTP {last_err.code}"
        log.warning("Telegram send failed after %s attempts: %s%s", _RETRIES, name, detail)


class CompositeNotifier:
    """Fan out to several notifiers. Failures in one never stop the others."""

    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    def send(self, text: str) -> None:
        for notifier in self._notifiers:
            try:
                notifier.send(text)
            except Exception as exc:
                log.warning(
                    "CompositeNotifier: %s failed: %s", type(notifier).__name__, type(exc).__name__
                )


def build_notifier(settings: Settings, *, async_outbox: bool = False) -> Notifier:
    """Build a notifier. CLI is sync; the serve loop passes async_outbox=True."""
    token = settings.telegram_bot_token.get_secret_value()
    chat = settings.telegram_chat_id
    if token and chat:
        return TelegramNotifier(token, chat, async_outbox=async_outbox)
    return NullNotifier()
