"""The money limits the operator can change from Telegram without a redeploy.

Four numbers: capital, risk per trade, the daily loss limit and the lot cap. Each one is resolved
in layers, a later layer winning:

1. the YAML files (`config/default.yaml`, `config/risk.yaml`),
2. the GitHub variables written to `.env` (`CAPITAL_INR`, `RISK_PER_TRADE_PCT`,
   `DAILY_LOSS_LIMIT_PCT`, `MAX_LOTS_PER_TRADE`; percentages are written as 1.5 for 1.5%),
3. what the operator set with `/capital`, `/riskpct`, `/dailyloss` or `/maxlots`, saved in
   `data/LIMITS_OVERRIDE.json` so a restart or a redeploy keeps it.

A change is made on the live config objects, so the next entry already uses it (no restart). Values
outside a sane range are refused, to catch a typing slip such as an extra zero. `/limits reset`
(or `reset` after one command) goes back to layers 1 and 2.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from orb_intraday_bot.config.settings import Settings, StrategyConfig
from orb_intraday_bot.risk.engine import RiskEngine

log = logging.getLogger("orb.limits")
OVERRIDE_FILE_NAME = "LIMITS_OVERRIDE.json"


@dataclass(frozen=True, slots=True)
class Knob:
    key: str
    command: str          # the Telegram command that sets it
    label: str
    unit: str             # "rs", "percent" or "count"
    low: float
    high: float
    env_name: str         # the GitHub variable (and .env name) that sets its default


KNOBS: dict[str, Knob] = {
    k.key: k
    for k in (
        Knob("capital", "/capital", "Capital", "rs", 10_000, 100_000_000, "CAPITAL_INR"),
        Knob(
            "risk_pct", "/riskpct", "Risk per trade", "percent", 0.05, 5.0,
            "RISK_PER_TRADE_PCT",
        ),
        Knob(
            "daily_loss_pct", "/dailyloss", "Daily loss limit", "percent", 0.1, 10.0,
            "DAILY_LOSS_LIMIT_PCT",
        ),
        Knob("max_lots", "/maxlots", "Max lots per trade", "count", 1, 50, "MAX_LOTS_PER_TRADE"),
    )
}

_SUFFIXES = (("crore", 1e7), ("cr", 1e7), ("lakh", 1e5), ("lac", 1e5), ("l", 1e5), ("k", 1e3))


def parse_number(text: str) -> float | None:
    """Read `750000`, `7,50,000`, `Rs 7.5L`, `1.5%` or `2k`. None when it is not a number."""
    cleaned = re.sub(r"(?i)rs\.?|inr|₹|%|,|\s", "", text)
    multiplier = 1.0
    for suffix, factor in _SUFFIXES:
        if cleaned.lower().endswith(suffix):
            cleaned, multiplier = cleaned[: -len(suffix)], factor
            break
    try:
        value = float(cleaned) * multiplier
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def describe_range(knob: Knob) -> str:
    return f"{format_value(knob, knob.low)} to {format_value(knob, knob.high)}"


class LimitControls:
    """Reads and changes the four limits on the live config and risk objects."""

    def __init__(self, cfg: StrategyConfig, risk: RiskEngine, data_dir: Path) -> None:
        self._cfg = cfg
        self._risk = risk
        self._data_dir = data_dir
        self._saved: dict[str, float] = {}
        self._baseline: dict[str, float] = {k: self.get(k) for k in KNOBS}

    # ---- reading -------------------------------------------------------------------------
    def get(self, key: str) -> float:
        if key == "capital":
            return float(self._cfg.sizing.capital_inr)
        if key == "risk_pct":
            return round(self._cfg.sizing.risk_pct_per_trade * 100, 6)
        if key == "daily_loss_pct":
            return round(self._risk.limits.daily_loss_limit_pct * 100, 6)
        return float(self._risk.limits.max_lots_per_trade)

    def default(self, key: str) -> float:
        """The value without the Telegram layer (YAML, then the GitHub variable)."""
        return self._baseline[key]

    def is_set_by_operator(self, key: str) -> bool:
        return key in self._saved

    def risk_budget_inr(self) -> float:
        return self.get("capital") * self.get("risk_pct") / 100

    def daily_loss_limit_inr(self) -> float:
        return self.get("capital") * self.get("daily_loss_pct") / 100

    # ---- changing ------------------------------------------------------------------------
    def _put(self, key: str, value: float) -> None:
        if key == "capital":
            self._cfg.sizing.capital_inr = float(value)
            self._risk.capital_inr = float(value)
        elif key == "risk_pct":
            self._cfg.sizing.risk_pct_per_trade = value / 100
        elif key == "daily_loss_pct":
            self._risk.limits.daily_loss_limit_pct = value / 100
        else:
            self._risk.limits.max_lots_per_trade = int(value)
        if key in ("capital", "daily_loss_pct"):
            self._risk.recheck_daily_loss()

    @staticmethod
    def check(key: str, value: float) -> str | None:
        """Why `value` is not acceptable for `key`, or None."""
        knob = KNOBS[key]
        if knob.unit == "count" and value != int(value):
            return f"{knob.label} must be a whole number."
        if not knob.low <= value <= knob.high:
            return (
                f"{knob.label} must be between {format_value(knob, knob.low)} and "
                f"{format_value(knob, knob.high)}."
            )
        return None

    def set(self, key: str, value: float) -> None:
        """Change a limit now and save it. Raises ValueError (a message to show) when refused."""
        problem = self.check(key, value)
        if problem:
            raise ValueError(problem)
        saved = {**self._saved, key: value}
        self._write(saved)
        self._saved = saved
        self._put(key, value)

    def reset(self, key: str | None = None) -> None:
        """Back to the default (YAML / GitHub variable) for one limit, or for all of them."""
        keys = list(KNOBS) if key is None else [key]
        saved = {k: v for k, v in self._saved.items() if k not in keys}
        self._write(saved)
        self._saved = saved
        for k in keys:
            self._put(k, self._baseline[k])

    def _write(self, values: dict[str, float]) -> None:
        path = self._data_dir / OVERRIDE_FILE_NAME
        try:
            if values:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(values, indent=1) + "\n", encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise ValueError(f"Could not save the change ({type(exc).__name__}).") from exc

    # ---- at start ------------------------------------------------------------------------
    def apply_configured(self, settings: Settings) -> list[str]:
        """Layer 2: the GitHub variables. Returns what was refused. Sets the new default."""
        wanted = {
            "capital": settings.capital_inr,
            "risk_pct": settings.risk_per_trade_pct,
            "daily_loss_pct": settings.daily_loss_limit_pct,
            "max_lots": settings.max_lots_per_trade,
        }
        problems: list[str] = []
        for key, value in wanted.items():
            if value is None:
                continue
            problem = self.check(key, float(value))
            if problem:
                problems.append(f"{KNOBS[key].env_name}={value} ignored: {problem}")
                continue
            self._put(key, float(value))
        self._baseline = {k: self.get(k) for k in KNOBS}
        return problems

    def load_saved(self) -> list[str]:
        """Layer 3: what the operator set earlier. Returns what could not be applied."""
        path = self._data_dir / OVERRIDE_FILE_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            return [f"{OVERRIDE_FILE_NAME} could not be read; the defaults are in force."]
        problems: list[str] = []
        if not isinstance(raw, dict):
            return [f"{OVERRIDE_FILE_NAME} is not valid; the defaults are in force."]
        for key, value in raw.items():
            if key not in KNOBS or not isinstance(value, int | float):
                continue
            problem = self.check(key, float(value))
            if problem:
                problems.append(f"saved {KNOBS[key].command} ignored: {problem}")
                continue
            self._saved[key] = float(value)
            self._put(key, float(value))
        return problems

    def startup_notes(self) -> list[str]:
        """Lines for the startup message: anything the operator changed that is still in force."""
        return [
            f"{KNOBS[k].label} {format_value(KNOBS[k], v)} set by Telegram "
            f"(default {format_value(KNOBS[k], self._baseline[k])})"
            for k, v in self._saved.items()
        ]


def format_value(knob: Knob, value: float) -> str:
    if knob.unit == "rs":
        return f"Rs {value:,.0f}"
    if knob.unit == "percent":
        return f"{value:g}%"
    return f"{value:g}"
