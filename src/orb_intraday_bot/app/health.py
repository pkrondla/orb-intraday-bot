"""Health services: is a run alive, and does the setup work (login, contract, chain, database)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from orb_intraday_bot.config.settings import StrategyConfig
from orb_intraday_bot.core.models import Direction
from orb_intraday_bot.data.contract import FuturesContract
from orb_intraday_bot.indicators.pivots import SessionLevels
from orb_intraday_bot.options.chain import OptionChain
from orb_intraday_bot.options.selector import kind_for, select_option
from orb_intraday_bot.runtime.monitor import ACTIVE_STATUSES, read_heartbeat
from orb_intraday_bot.storage.store import TradeStore

STALE_AFTER_SEC = 120


def _check_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    low = str(exc).lower()
    if "permission" in low or "authorisation" in low or "authorization" in low:
        text += (
            " A key that works on your PC often fails on EC2 until the instance "
            "public IP is allowed on that Groww API key."
        )
    return text


@dataclass(frozen=True)
class StatusReport:
    text: str
    exit_code: int


def status_report(heartbeat_path: Path, now: datetime) -> StatusReport:
    info = read_heartbeat(heartbeat_path)
    if info is None:
        return StatusReport(
            "No heartbeat yet: the bot has not run (or the data folder is different).", 1
        )
    age = info.age_sec(now)
    alive = info.status in ACTIVE_STATUSES and age < STALE_AFTER_SEC
    if alive:
        verdict = "ALIVE"
    elif info.status in ACTIVE_STATUSES:
        verdict = "STALE - process stuck or dead?"
    else:
        verdict = "not running"
    lines = [
        f"status: {info.status} ({verdict})  heartbeat {age:.0f}s ago  pid {info.pid}",
        f"last candle: {info.last_candle or '-'}  open positions: {info.open_positions}  "
        f"errors: {info.errors}",
    ]
    if info.message:
        lines.append(f"message: {info.message}")
    ok = alive or info.status in {"finished", "stopped"}
    return StatusReport("\n".join(lines), 0 if ok else 1)


class CheckData(Protocol):
    def futures_contract(self, today: date | None = None) -> FuturesContract: ...

    def previous_session_levels(
        self, contract: FuturesContract, today: date, lookback_days: int = 10
    ) -> SessionLevels: ...

    def nearest_option_expiry(self, today: date | None = None) -> date: ...

    def ltp(self, trading_symbols: Sequence[str]) -> dict[str, float]: ...

    def option_chain(self, expiry: date) -> OptionChain: ...


@dataclass(frozen=True)
class CheckStep:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"  {'OK  ' if self.ok else 'FAIL'}  {self.name}: {self.detail}"


def run_checks(
    data: CheckData,
    cfg: StrategyConfig,
    database: Path,
    today: date,
    on_step: Callable[[CheckStep], None] = lambda step: None,
) -> list[CheckStep]:
    """Each step is tried on its own; a failure is reported, not raised. The first step (login
    and contract) is required for the rest, so its failure ends the run."""
    steps: list[CheckStep] = []

    def step(name: str, fn: Callable[[], str]) -> bool:
        try:
            result = CheckStep(name, True, fn())
        except Exception as exc:
            result = CheckStep(name, False, _check_error(exc))
        steps.append(result)
        on_step(result)
        return result.ok

    contracts: list[FuturesContract] = []

    def connect() -> str:
        found = data.futures_contract(today)
        contracts.append(found)
        return f"{found.trading_symbol}, expiry {found.expiry}, lot {found.lot_size}"

    if not step("login and contract", connect):
        return steps
    contract = contracts[0]

    def pivots() -> str:
        levels = data.previous_session_levels(contract, today)
        p = levels.pivots()
        return (
            f"{levels.day}: {levels.describe()}; "
            f"S1 {p.s1:.1f} P {p.p:.1f} R1 {p.r1:.1f}"
        )

    step("previous session and pivots", pivots)
    step("nearest weekly expiry", lambda: str(data.nearest_option_expiry(today)))

    def chain() -> str:
        expiry_date = data.nearest_option_expiry(today)
        price = data.ltp([contract.trading_symbol])[contract.trading_symbol]
        chain_data = data.option_chain(expiry_date)
        parts = []
        for direction in (Direction.LONG, Direction.SHORT):
            q = select_option(direction, price, chain_data, cfg.options).quote
            parts.append(
                f"{kind_for(direction).value} {q.trading_symbol} delta {q.delta} ltp {q.ltp}"
            )
        return f"futures {price}; " + "; ".join(parts)

    step("option chain and strike pick", chain)

    def database_step() -> str:
        TradeStore(database).close()
        return str(database)

    step("database", database_step)
    return steps
