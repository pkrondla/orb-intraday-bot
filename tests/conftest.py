"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from orb_intraday_bot import cli
from orb_intraday_bot.app.context import AppContext
from orb_intraday_bot.config.settings import Settings, StrategyConfig
from tests.runtime_support import CFG


@pytest.fixture
def cli_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[StrategyConfig | None], AppContext]:
    """Point every CLI command at `tmp_path` instead of the real data folder and `.env`.

    Call the fixture to get the context (optionally with another config).
    """

    def install(cfg: StrategyConfig | None = None) -> AppContext:
        ctx = AppContext(Settings.model_construct(), cfg or CFG, tmp_path)
        monkeypatch.setattr(cli, "load_context", lambda: ctx)
        return ctx

    return install
