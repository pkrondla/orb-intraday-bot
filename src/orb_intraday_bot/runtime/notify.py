"""Alerts. Optional Telegram, never able to stop or slow the bot.

Telegram is off unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set. Only trade alerts, exits,
the daily summary and stall alerts are sent (and candle reports if `notify_candle_reports` is
on). A failed send is logged and ignored.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from typing import Protocol

from orb_intraday_bot.config.settings import Settings

log = logging.getLogger("orb.notify")
_TIMEOUT_SEC = 10
_MAX_LEN = 3900          # Telegram's limit is 4096 characters per message


class Notifier(Protocol):
    def send(self, text: str) -> None: ...


class NullNotifier:
    def send(self, text: str) -> None:
        return None


PostFunc = Callable[[str, bytes], None]


def _urllib_post(url: str, body: bytes) -> None:
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:   # noqa: S310
        response.read()


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, post: PostFunc = _urllib_post) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._post = post

    def send(self, text: str) -> None:
        body = json.dumps({"chat_id": self._chat_id, "text": text[:_MAX_LEN]}).encode("utf-8")
        try:
            self._post(self._url, body)
        except Exception as exc:        # never let an alert failure reach the trading loop
            # The URL holds the bot token, so only the error type and a short message are logged.
            log.warning("Telegram send failed: %s", type(exc).__name__)


def build_notifier(settings: Settings) -> Notifier:
    token = settings.telegram_bot_token.get_secret_value()
    chat = settings.telegram_chat_id
    if token and chat:
        return TelegramNotifier(token, chat)
    return NullNotifier()
