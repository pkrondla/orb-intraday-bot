"""Which config produced a trade: three short hashes, one per concern.

`StrategyConfig` is one YAML file but holds three kinds of settings that change for different
reasons, so each gets its own hash:

    strategy    the rules: what is traded, stops, targets, sizing, setups, arbiter. A change here
                changes the trades and starts a new forward test.
    assumptions what the paper engine pretends: slippage and charges. Changes the P&L only.
    runtime     timing, polling, alerts. Changes nothing about the trades.

Every position records the `strategy` hash it was opened under, so a result can always be tied
to the exact rules. A test makes sure a new config section must be assigned to a group.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from orb_intraday_bot.config.settings import StrategyConfig

STRATEGY_SECTIONS: tuple[str, ...] = (
    "instrument", "session", "stops", "targets", "options", "sizing", "consolidation",
    "entries", "whipsaw", "structure", "fake_break", "arbiter", "enabled_setups",
)
ASSUMPTION_SECTIONS: tuple[str, ...] = ("paper",)
RUNTIME_SECTIONS: tuple[str, ...] = ("runtime",)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    strategy: str
    assumptions: str
    runtime: str


def _digest(config: StrategyConfig, sections: tuple[str, ...]) -> str:
    dumped = config.model_dump(mode="json")
    payload = json.dumps({s: dumped[s] for s in sections}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def fingerprint(config: StrategyConfig) -> Fingerprint:
    return Fingerprint(
        strategy=_digest(config, STRATEGY_SECTIONS),
        assumptions=_digest(config, ASSUMPTION_SECTIONS),
        runtime=_digest(config, RUNTIME_SECTIONS),
    )
