"""Telegram command poller: parse, authorize, queue."""

from __future__ import annotations

import json
from datetime import date

from orb_intraday_bot.runtime.telegram_commands import (
    CommandBus,
    CommandKind,
    TelegramCommandPoller,
    chats_match,
    normalize_chat_id,
    parse_command,
)


def test_parse_commands() -> None:
    assert parse_command("/status").kind is CommandKind.STATUS
    assert parse_command("/help@MyBot").kind is CommandKind.HELP
    assert parse_command("/flatten").kind is CommandKind.FLATTEN
    assert parse_command("/flatten ab12").kind is CommandKind.FLATTEN   # no code is needed
    assert parse_command("/stop").kind is CommandKind.STOP_TODAY
    assert parse_command("/pause").kind is CommandKind.STOP_TODAY
    assert parse_command("/start").kind is CommandKind.START_TODAY
    assert parse_command("/resume").kind is CommandKind.START_TODAY
    assert parse_command("/arm dead").kind is CommandKind.UNKNOWN      # no daily arm any more
    assert parse_command("hello").kind is CommandKind.UNKNOWN


def test_bus_stop_is_per_day() -> None:
    bus = CommandBus()
    day = date(2026, 10, 16)
    assert not bus.is_stopped(day)
    bus.stop_for(day)
    assert bus.is_stopped(day) and not bus.is_stopped(date(2026, 10, 19))
    bus.start_for(day)
    assert not bus.is_stopped(day)


def test_poller_ignores_unauthorized_chat() -> None:
    replies: list[str] = []
    bus = CommandBus()
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {"chat": {"id": 999}, "text": "/status"},
            }
        ],
    }

    def get(_url: str) -> bytes:
        get.calls += 1  # type: ignore[attr-defined]
        if get.calls > 1:  # type: ignore[attr-defined]
            raise SystemExit  # stop the loop in tests via exception path
        return json.dumps(payload).encode()

    get.calls = 0  # type: ignore[attr-defined]
    poller = TelegramCommandPoller(
        "tok", "42", bus, replies.append, get=get, sleep=lambda _: None
    )
    # Drive one poll directly
    poller._poll_once()  # noqa: SLF001
    assert bus.drain() == []
    assert replies == []


def test_poller_queues_authorized_commands_and_handles_help() -> None:
    replies: list[str] = []
    bus = CommandBus()
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 42}, "text": "/help"}},
        {"update_id": 11, "message": {"chat": {"id": 42}, "text": "/pause"}},
        {"update_id": 12, "message": {"chat": {"id": 42}, "text": "/flatten"}},
    ]
    state = {"i": 0}

    def get(_url: str) -> bytes:
        i = state["i"]
        state["i"] += 1
        if i >= len(updates):
            return json.dumps({"ok": True, "result": []}).encode()
        return json.dumps({"ok": True, "result": [updates[i]]}).encode()

    poller = TelegramCommandPoller(
        "tok", "42", bus, replies.append, get=get, sleep=lambda _: None
    )
    poller._poll_once()  # noqa: SLF001
    poller._poll_once()  # noqa: SLF001
    poller._poll_once()  # noqa: SLF001
    assert any("ORB bot commands" in r for r in replies)
    assert not any("code" in r.lower() for r in replies if "ORB bot commands" not in r)
    cmds = bus.drain()
    assert [c.kind for c in cmds] == [CommandKind.STOP_TODAY, CommandKind.FLATTEN]


def test_chat_ids_match_after_quotes_and_spaces() -> None:
    assert normalize_chat_id(' "1447109810" ') == "1447109810"
    assert chats_match(1447109810, "1447109810")
    assert chats_match("'42'", "42")
    assert not chats_match("42", "43")


def test_poller_accepts_quoted_chat_id_and_operator_sender() -> None:
    replies: list[str] = []
    bus = CommandBus()
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/help"}},
        {
            "update_id": 2,
            "message": {
                "chat": {"id": -1001},
                "from": {"id": 42},
                "text": "/help",
            },
        },
    ]
    state = {"i": 0}

    def get(_url: str) -> bytes:
        i = state["i"]
        state["i"] += 1
        if i >= len(updates):
            return json.dumps({"ok": True, "result": []}).encode()
        return json.dumps({"ok": True, "result": [updates[i]]}).encode()

    poller = TelegramCommandPoller(
        "tok", ' "42" ', bus, replies.append, get=get, sleep=lambda _: None
    )
    poller._poll_once()  # noqa: SLF001
    poller._poll_once()  # noqa: SLF001
    assert len([r for r in replies if "ORB bot commands" in r]) == 2
