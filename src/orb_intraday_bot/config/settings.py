"""Runtime settings (environment) and strategy config (YAML)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment-level settings. Secrets live here, never in YAML."""

    model_config = SettingsConfigDict(
        # Later files override earlier ones. Both locations are supported.
        env_file=(_PROJECT_ROOT / ".env", _PROJECT_ROOT / "config" / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # TOTP token + TOTP secret. The bot generates the 6-digit code and refreshes the token itself.
    groww_totp_token: SecretStr = Field(default=SecretStr(""), alias="GROWW_TOTP_TOKEN")
    groww_totp_secret: SecretStr = Field(default=SecretStr(""), alias="GROWW_TOTP_SECRET")
    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    token_cooldown_sec: int = Field(default=180, ge=60, alias="GROWW_TOKEN_COOLDOWN_SEC")
    strategy_config_path: Path = Field(
        default=Path("config/default.yaml"), alias="STRATEGY_CONFIG_PATH"
    )
    # Optional Telegram alerts. Leave empty to disable. Only trade alerts and summaries are sent.
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    # Optional dead-man ping URL (e.g. healthchecks.io). Watchdog hits it when healthy.
    healthcheck_url: str = Field(default="", alias="HEALTHCHECK_URL")
    # paper | shadow | live. Live still needs LIVE_ARMED and credentials.
    live_mode: Literal["paper", "shadow", "live"] = Field(default="paper", alias="LIVE_MODE")
    live_armed: bool = Field(default=False, alias="LIVE_ARMED")
    # True (the default) runs the whole live pipeline but skips the broker order step: orders are
    # simulated from real bid/ask quotes. False sends real orders (live mode only).
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    # Optional override for live trading. Empty = reuse GROWW_TOTP_TOKEN / GROWW_TOTP_SECRET.
    groww_trading_totp_token: SecretStr = Field(
        default=SecretStr(""), alias="GROWW_TRADING_TOTP_TOKEN"
    )
    groww_trading_totp_secret: SecretStr = Field(
        default=SecretStr(""), alias="GROWW_TRADING_TOTP_SECRET"
    )

    # Optional defaults for the money limits (GitHub variables). Empty or unset = the YAML value.
    # Percentages are written as 1.5 for 1.5%. /capital, /riskpct, /dailyloss and /maxlots on
    # Telegram override these without a redeploy (see runtime/limit_overrides.py).
    capital_inr: float | None = Field(default=None, alias="CAPITAL_INR")
    risk_per_trade_pct: float | None = Field(default=None, alias="RISK_PER_TRADE_PCT")
    daily_loss_limit_pct: float | None = Field(default=None, alias="DAILY_LOSS_LIMIT_PCT")
    max_lots_per_trade: int | None = Field(default=None, alias="MAX_LOTS_PER_TRADE")

    @field_validator(
        "capital_inr", "risk_per_trade_pct", "daily_loss_limit_pct", "max_lots_per_trade",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("telegram_chat_id", mode="before")
    @classmethod
    def _strip_chat_id(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        text = value.strip().strip('"').strip("'")
        try:
            return str(int(text)) if text else ""
        except ValueError:
            return text

    @field_validator("telegram_bot_token", mode="before")
    @classmethod
    def _strip_bot_token(cls, value: object) -> object:
        if isinstance(value, SecretStr):
            value = value.get_secret_value()
        if isinstance(value, str):
            return value.strip().strip('"').strip("'").replace("\\n", "").replace("\\r", "")
        return value

    def has_trading_credentials(self) -> bool:
        trading = (
            self.groww_trading_totp_token.get_secret_value()
            and self.groww_trading_totp_secret.get_secret_value()
        )
        shared = (
            self.groww_totp_token.get_secret_value()
            and self.groww_totp_secret.get_secret_value()
        )
        return bool(trading or shared)


class InstrumentConfig(BaseModel):
    underlying: Literal["NIFTY"] = "NIFTY"
    exchange: str = "NSE"
    segment: str = "FNO"
    futures_tick_size: float = 0.1
    option_tick_size: float = 0.05
    roll_days_before_expiry: int = Field(default=0, ge=0)


class SessionConfig(BaseModel):
    open: str = "09:15"
    close: str = "15:30"
    candle_minutes: Literal[3] = 3
    candle_publish_delay_sec: int = Field(default=5, ge=0)


class StopsConfig(BaseModel):
    hard_stop_points: float = Field(default=35, gt=0)
    min_stop_points: float = Field(default=10, gt=0)
    option_fallback_points: float = Field(default=20, gt=0)


class TargetsConfig(BaseModel):
    t1_rule: Literal["nearest_pivot_else_half"] = "nearest_pivot_else_half"
    t1_book_fraction: float = Field(default=0.5, gt=0, lt=1)
    move_sl_to_entry_after_t1: bool = True
    # Pull T1 in to at most this many points from the entry (None = no cap; the nearest pivot can
    # be 200+ points away). Off by default: a tuning option for Phase 9.
    t1_max_distance_points: float | None = Field(default=None, gt=0)


class OptionsConfig(BaseModel):
    strike_step: int = Field(default=50, gt=0)
    delta_min: float = Field(default=0.50, gt=0, lt=1)
    delta_max: float = Field(default=0.60, gt=0, lt=1)
    delta_target: float = Field(default=0.55, gt=0, lt=1)
    candidates: list[Literal["atm", "itm1"]] = ["atm", "itm1"]


class SizingConfig(BaseModel):
    capital_inr: float = Field(default=500_000, gt=0)
    risk_pct_per_trade: float = Field(default=0.01, gt=0, le=1)
    max_open_positions: int = Field(default=5, ge=1)
    # What the lot count is sized on. "stop_plan" (chosen rule): the distance to the structural
    # level (floor 10, cap 35), while the trade's stop stays 35 points. "hard_stop": the real
    # stop distance, which keeps the loss at the stop inside the budget.
    risk_basis: Literal["stop_plan", "hard_stop"] = "stop_plan"


class ConsolidationConfig(BaseModel):
    min_touches: int = Field(default=3, ge=2)
    min_minutes: int = 45
    max_minutes: int = 60


class WhipsawConfig(BaseModel):
    """Additional whipsaws after an ORB or trap confirmation."""

    min_rejections: int = Field(default=2, ge=2)   # [assumption] "two or more rejections"


class StructureConfig(BaseModel):
    """2LL/3LL (inside a rise) and 2HH/3HH (inside a fall) continuation whipsaws."""

    min_count: int = Field(default=2, ge=2)
    max_count: int = Field(default=3, ge=2)
    min_rise_points: float = Field(default=20, gt=0)   # [assumption] ignore noise-sized rises


class FakeBreakConfig(BaseModel):
    """Consolidation + fake breakdown (long) / fake breakout (short). All [assumption]."""

    consolidation_candles: int = Field(default=10, ge=4)    # candles that form the range
    max_range_points: float = Field(default=45, gt=0)       # widest range still "consolidation"
    edge_touches: int = Field(default=2, ge=1)              # touches of each edge of the range
    max_fake_candles: int = Field(default=3, ge=1)          # candles allowed beyond the range
    min_reversal_leg_points: float = Field(default=20, gt=0)  # "sharp" reversal leg
    max_wait_candles: int = Field(default=10, ge=1)         # wait for the 50% retracement


class EntriesConfig(BaseModel):
    orb_entry_reference: Literal["previous_candle"] = "previous_candle"
    orb_fill_rule: Literal["touch_else_confirmation_close"] = "touch_else_confirmation_close"
    fake_breakout_entry_retracement: float = Field(default=0.5, gt=0, lt=1)
    # How close (futures points) a low may come to ORH, or a high to ORL, and still count as the
    # retest. Absorbs tick differences between data feeds (Groww vs TradingView differ ~0.7 pt).
    retest_tolerance_points: float = Field(default=1.0, ge=0)


class ArbiterConfig(BaseModel):
    """How competing signals on the same candle, and signals while trades are open, are handled."""

    # Highest priority first. [assumption]: the OR-based setups come before the minor-price-action
    # ones; the notes do not rank setups.
    priority: list[str] = [
        "ORB_BREAKOUT",
        "PIVOT_BREAKOUT",
        "TRAP_REVERSE_RETEST",
        "FOLLOWUP_CASE_1",
        "FOLLOWUP_CASE_2",
        "CONSOLIDATION_BREAKOUT",
        "ADDITIONAL_WHIPSAW",
        "FAKE_BREAK",
        "STRUCTURE_WHIPSAW",
    ]
    allow_opposite_positions: bool = False        # [assumption] one instrument, one direction
    max_trades_per_day: int = Field(default=6, ge=1)             # [assumption]
    max_structure_trades_per_day: int = Field(default=2, ge=0)   # [assumption] 2LL/3LL/2HH/3HH


class ChargesConfig(BaseModel):
    """Option trading charges, as fractions of premium turnover. [All assumptions: check them
    against a Groww contract note and update here.]"""

    brokerage_flat_inr: float = Field(default=20.0, ge=0)      # per executed order
    brokerage_pct: float = Field(default=0.001, ge=0)          # the lower of flat and this
    stt_sell_pct: float = Field(default=0.0015, ge=0)          # sell side only
    exchange_txn_pct: float = Field(default=0.0003503, ge=0)   # both sides
    sebi_pct: float = Field(default=0.000001, ge=0)            # both sides
    stamp_buy_pct: float = Field(default=0.00003, ge=0)        # buy side only
    gst_pct: float = Field(default=0.18, ge=0)                 # on brokerage + exchange + SEBI


class PaperConfig(BaseModel):
    # Option points lost on every fill (paid on entry, given up on exit). [assumption] 0 = fills
    # at the quoted option price; raise it to see how sensitive results are to slippage.
    slippage_option_points: float = Field(default=0.0, ge=0)
    charges: ChargesConfig = ChargesConfig()
    database_file: str = "paper_trades.sqlite"     # inside DATA_DIR


class RuntimeConfig(BaseModel):
    """Live run timing and monitoring. Not strategy rules."""

    poll_interval_sec: int = Field(default=15, ge=5)        # price polls while a position is open
    fetch_retry_sec: int = Field(default=5, ge=1)           # retry while a closed candle is late
    max_signal_age_sec: int = Field(default=120, ge=0)      # older signals are not opened
    stall_alert_sec: int = Field(default=180, ge=30)        # no new candle for this long: alert
    no_session_exit_time: str = "09:45"                     # no candles by then: holiday, exit
    heartbeat_file: str = "heartbeat.json"                  # inside DATA_DIR
    notify_candle_reports: bool = False                     # send every candle report to Telegram


class EnabledSetups(BaseModel):
    orb: bool = True
    trap: bool = True
    followup_failure: bool = True
    additional_whipsaw: bool = True
    consolidation_breakout: bool = True
    whipsaw_structure: bool = True
    fake_breakout: bool = True
    both_sides_trap: bool = False


class StrategyConfig(BaseModel):
    instrument: InstrumentConfig = InstrumentConfig()
    session: SessionConfig = SessionConfig()
    stops: StopsConfig = StopsConfig()
    targets: TargetsConfig = TargetsConfig()
    options: OptionsConfig = OptionsConfig()
    sizing: SizingConfig = SizingConfig()
    consolidation: ConsolidationConfig = ConsolidationConfig()
    entries: EntriesConfig = EntriesConfig()
    whipsaw: WhipsawConfig = WhipsawConfig()
    structure: StructureConfig = StructureConfig()
    fake_break: FakeBreakConfig = FakeBreakConfig()
    arbiter: ArbiterConfig = ArbiterConfig()
    paper: PaperConfig = PaperConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    enabled_setups: EnabledSetups = EnabledSetups()


def load_strategy_config(path: Path) -> StrategyConfig:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return StrategyConfig.model_validate(raw)


def apply_overrides(config: StrategyConfig, overrides: Sequence[str]) -> StrategyConfig:
    """A copy of `config` with `section.key=value` overrides applied (for tuning experiments).

    Values are read as JSON (`35`, `0.5`, `true`, `null`, `[1,2]`), otherwise as plain text. The
    result is validated, so an unknown key or a bad value raises instead of being ignored.
    """
    data = config.model_dump(mode="json")
    for item in overrides:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"override must look like section.key=value, got {item!r}")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        *path, leaf = key.strip().split(".")
        node = data
        for part in path:
            if not isinstance(node.get(part), dict):
                raise ValueError(f"unknown config section in {key!r}")
            node = node[part]
        if leaf not in node:
            raise ValueError(f"unknown config key {key!r}")
        node[leaf] = value
    return StrategyConfig.model_validate(data)
