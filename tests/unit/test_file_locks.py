"""A locked file (OneDrive sync, antivirus, a viewer) must never stop a trading run.

Seen live on 16 Oct 2026: `heartbeat.tmp -> heartbeat.json` failed with "access denied" and the
run ended after a few candles.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from orb_intraday_bot.runtime.monitor import read_heartbeat, write_heartbeat
from orb_intraday_bot.runtime.output import make_output
from tests.runtime_support import Harness, at


def _write(path: Path, sleeps: list[float] | None = None) -> bool:
    return write_heartbeat(
        path, now=at(9, 30), status="running", last_candle=None, open_positions=0, errors=0,
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
    )


def _deny_rename(monkeypatch: pytest.MonkeyPatch, times: int) -> dict[str, int]:
    calls = {"n": 0}
    real = Path.replace

    def replace(self: Path, target: Path | str) -> Path:
        calls["n"] += 1
        if calls["n"] <= times:
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    return calls


def test_a_briefly_locked_heartbeat_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hb.json"
    calls = _deny_rename(monkeypatch, times=2)
    sleeps: list[float] = []
    assert _write(path, sleeps)
    assert calls["n"] == 3 and len(sleeps) == 2
    assert read_heartbeat(path) is not None


def test_a_permanently_locked_rename_falls_back_to_writing_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hb.json"
    _deny_rename(monkeypatch, times=10_000)
    assert _write(path)
    info = read_heartbeat(path)
    assert info is not None and info.status == "running"


def test_a_heartbeat_that_cannot_be_written_at_all_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def deny(*_: object, **__: object) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Path, "write_text", deny)
    with caplog.at_level(logging.WARNING, logger="orb.monitor"):
        assert _write(tmp_path / "hb.json") is False
    assert "Heartbeat not written" in caplog.text


def test_a_whole_session_survives_a_locked_heartbeat_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_rename(monkeypatch, times=10_000)
    monkeypatch.setattr("orb_intraday_bot.runtime.monitor.time.sleep", lambda _: None)
    h = Harness(tmp_path, at(9, 10))
    outcome = h.runner().run()
    assert outcome.exit_code == 0
    assert "OPENED" in h.text and h.store.positions_for_day(date(2026, 10, 15))


def test_a_locked_log_file_does_not_stop_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = make_output(tmp_path, date(2026, 10, 16), "RUN-1")
    real_open = Path.open

    def locked(self: Path, *args: object, **kwargs: object) -> object:
        if self.suffix == ".log":
            raise PermissionError(5, "Access is denied")
        return real_open(self, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(Path, "open", locked)
    out("still printed")
    assert "still printed" in capsys.readouterr().out
    for handler in list(logging.getLogger("orb").handlers):
        logging.getLogger("orb").removeHandler(handler)
        handler.close()
