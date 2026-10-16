"""The composition root: settings, strategy config and the paths every command needs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orb_intraday_bot.adapters.groww.market_data import GrowwMarketData
from orb_intraday_bot.config import Settings
from orb_intraday_bot.config.settings import _PROJECT_ROOT, StrategyConfig, load_strategy_config
from orb_intraday_bot.runtime.guard import run_startup_checks


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    cfg: StrategyConfig
    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.cfg.paper.database_file

    def history_path(self, symbol: str) -> Path:
        return self.data_dir / "history" / f"{symbol}.json"


def _resolve(path: Path) -> Path:
    return path if path.is_absolute() else _PROJECT_ROOT / path


def load_context() -> AppContext:
    """Read `.env` and the strategy YAML, and refuse to continue unless the safety checks pass."""
    settings = Settings()
    run_startup_checks(settings)
    settings.data_dir = _resolve(settings.data_dir)
    cfg = load_strategy_config(_resolve(settings.strategy_config_path))
    return AppContext(settings, cfg, settings.data_dir)


def build_market_data(ctx: AppContext) -> GrowwMarketData:
    from orb_intraday_bot.adapters.groww.factory import build_readonly_client

    return GrowwMarketData(build_readonly_client(ctx.settings), ctx.cfg.instrument)
