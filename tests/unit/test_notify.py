"""Telegram notifier: split, retry, outbox, composite, never raises."""

from __future__ import annotations

import json
from typing import Any

from orb_intraday_bot.config.settings import Settings
from orb_intraday_bot.runtime.notify import (
    CompositeNotifier,
    NullNotifier,
    TelegramNotifier,
    as_telegram_html,
    build_notifier,
    split_message,
)


def test_split_message_keeps_short_text() -> None:
    assert split_message("hello") == ["hello"]
    assert split_message("") == []


def test_split_message_breaks_on_newlines() -> None:
    text = ("line\n" * 900)
    parts = split_message(text, max_len=100)
    assert len(parts) > 1
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")


def test_split_message_hard_cuts_when_no_newline() -> None:
    text = "x" * 250
    parts = split_message(text, max_len=100)
    assert parts == ["x" * 100, "x" * 100, "x" * 50]


class _FakePost:
    def __init__(self, fails: int = 0) -> None:
        self.fails = fails
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sleeps: list[float] = []

    def __call__(self, url: str, body: bytes) -> bytes:
        payload = json.loads(body.decode("utf-8"))
        self.calls.append((url, payload))
        if self.fails > 0:
            self.fails -= 1
            raise TimeoutError("slow")
        return b'{"ok":true}'

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def test_telegram_html_sets_parse_mode() -> None:
    fake = _FakePost()
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=False)
    n.send(as_telegram_html("<b>BUY</b>"))
    payload = fake.calls[0][1]
    assert payload["text"] == "<b>BUY</b>"
    assert payload["parse_mode"] == "HTML"
    assert payload["disable_web_page_preview"] is True


def test_telegram_send_posts_json_to_bot_api() -> None:
    fake = _FakePost()
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=False)
    n.send("hello")
    assert len(fake.calls) == 1
    url, payload = fake.calls[0]
    assert url.endswith("/bottok/sendMessage")
    assert payload == {"chat_id": "42", "text": "hello"}


def test_telegram_retries_then_succeeds() -> None:
    fake = _FakePost(fails=2)
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=False)
    n.send("retry me")
    assert len(fake.calls) == 3
    assert fake.sleeps == [0.5, 1.5]


def test_telegram_failure_never_raises() -> None:
    fake = _FakePost(fails=10)
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=False)
    n.send("will fail")   # must not raise
    assert len(fake.calls) == 3


def test_telegram_splits_long_messages() -> None:
    fake = _FakePost()
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=False)
    n.send("y" * 8000)
    assert len(fake.calls) >= 2
    assert all(len(p["text"]) <= 3900 for _, p in fake.calls)


def test_async_outbox_delivers_and_close_flushes() -> None:
    fake = _FakePost()
    n = TelegramNotifier("tok", "42", post=fake, sleep=fake.sleep, async_outbox=True)
    n.send("async")
    n.close(timeout=2.0)
    assert any(p["text"] == "async" for _, p in fake.calls)


def test_composite_fans_out_and_swallows_errors() -> None:
    good = _FakePost()
    bad_calls: list[str] = []

    class Boom:
        def send(self, text: str) -> None:
            bad_calls.append(text)
            raise RuntimeError("nope")

    CompositeNotifier(
        [TelegramNotifier("t", "1", post=good, sleep=good.sleep, async_outbox=False), Boom()]
    ).send("hi")
    assert good.calls and bad_calls == ["hi"]


def test_build_notifier_null_without_credentials() -> None:
    assert isinstance(build_notifier(Settings(), async_outbox=False), NullNotifier)


def test_format_telegram_http_error_hides_token() -> None:
    from urllib.error import URLError

    from orb_intraday_bot.runtime.notify import format_telegram_http_error, ssl_context

    err = URLError("CERTIFICATE_VERIFY_FAILED token=secret-token")
    text = format_telegram_http_error(err, "secret-token")
    assert "URLError" in text and "CERTIFICATE_VERIFY_FAILED" in text
    assert "secret-token" not in text
    assert ssl_context().cert_store_stats()["x509_ca"] > 0


def test_build_notifier_telegram_when_configured() -> None:
    from pydantic import SecretStr

    settings = Settings.model_construct(
        telegram_bot_token=SecretStr("abc"), telegram_chat_id="99"
    )
    n = build_notifier(settings, async_outbox=False)
    assert isinstance(n, TelegramNotifier)
