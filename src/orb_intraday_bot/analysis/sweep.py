"""Compare configuration variants on the same candles (tuning the [assumption] values).

Each variant is a name and a list of `section.key=value` overrides applied to the base config.
Everything runs on the cached candles, so a sweep needs no network once the history is cached.

Read the table with care: a replay covers a few weeks of one market regime, and the more
variants are tried the more likely one looks good by luck. Prefer changes that help broadly (most
variants of the same idea, most weeks) over the single best row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from orb_intraday_bot.analysis.backtest import DayResult, run_range
from orb_intraday_bot.analysis.stats import Overall, overall, rows_from_results
from orb_intraday_bot.config.settings import StrategyConfig, apply_overrides
from orb_intraday_bot.core.models import Candle
from orb_intraday_bot.indicators.pivots import SessionLevels


@dataclass(frozen=True, slots=True)
class Variant:
    name: str
    overrides: tuple[str, ...] = ()


# Each group changes one assumption at a time against the baseline.
DEFAULT_VARIANTS: tuple[Variant, ...] = (
    Variant("baseline"),
    Variant("T1 cap 50", ("targets.t1_max_distance_points=50",)),
    Variant("T1 cap 75", ("targets.t1_max_distance_points=75",)),
    Variant("T1 cap 100", ("targets.t1_max_distance_points=100",)),
    Variant("T1 cap 150", ("targets.t1_max_distance_points=150",)),
    Variant("structure off", ("enabled_setups.whipsaw_structure=false",)),
    Variant("structure rise 10", ("structure.min_rise_points=10",)),
    Variant("structure rise 30", ("structure.min_rise_points=30",)),
    Variant("structure rise 40", ("structure.min_rise_points=40",)),
    Variant("structure max 1/day", ("arbiter.max_structure_trades_per_day=1",)),
    Variant("structure max 3/day", ("arbiter.max_structure_trades_per_day=3",)),
    Variant("retest tol 0", ("entries.retest_tolerance_points=0",)),
    Variant("retest tol 2", ("entries.retest_tolerance_points=2",)),
    Variant("whipsaw min rej 3", ("whipsaw.min_rejections=3",)),
    Variant("max trades/day 4", ("arbiter.max_trades_per_day=4",)),
    Variant("max trades/day 8", ("arbiter.max_trades_per_day=8",)),
    Variant("size on real stop", ("sizing.risk_basis=hard_stop",)),
    Variant("slippage 1 pt", ("paper.slippage_option_points=1",)),
    Variant("slippage 2 pt", ("paper.slippage_option_points=2",)),
    Variant("no break-even stop", ("targets.move_sl_to_entry_after_t1=false",)),
    Variant("T1 books 30%", ("targets.t1_book_fraction=0.3",)),
    Variant("T1 books 70%", ("targets.t1_book_fraction=0.7",)),
)


def parse_variant(text: str) -> Variant:
    """`name|key=value,key=value` (the name alone is allowed for the baseline)."""
    name, _, rest = text.partition("|")
    overrides = tuple(p.strip() for p in rest.split(",") if p.strip())
    return Variant(name.strip() or "variant", overrides)


@dataclass(frozen=True, slots=True)
class VariantResult:
    variant: Variant
    stats: Overall
    results: list[DayResult]


def run_sweep(
    base: StrategyConfig,
    variants: Iterable[Variant],
    candles: Sequence[Candle],
    symbol: str,
    lot_size: int,
    first: date | None,
    last: date | None,
    only: set[date] | None,
    today: date | None,
    min_candles: int,
    progress: Callable[[str], None] | None = None,
    levels: Mapping[date, SessionLevels] | None = None,
) -> list[VariantResult]:
    out = []
    for variant in variants:
        if progress is not None:
            progress(variant.name)
        cfg = apply_overrides(base, variant.overrides)
        results = run_range(
            cfg, candles, symbol, lot_size, first, last, only=only, today=today,
            min_candles=min_candles, levels=levels,
        )
        out.append(
            VariantResult(
                variant, overall(rows_from_results(results), len(results)), results
            )
        )
    return out


def format_sweep(results: Sequence[VariantResult]) -> str:
    if not results:
        return "No variants."
    base = results[0].stats.group
    width = max(len(r.variant.name) for r in results)
    head = (
        f"  {'variant':<{width}} {'n':>4} {'win':>5} {'points':>9} {'d pts':>8} {'avgR':>6} "
        f"{'PF':>5} {'net Rs':>10} {'maxDD Rs':>10} {'streak':>6}"
    )
    lines = [head]
    for r in results:
        g, o = r.stats.group, r.stats
        pf = "-" if g.profit_factor is None else f"{g.profit_factor:.2f}"
        lines.append(
            f"  {r.variant.name:<{width}} {g.trades:>4} {g.win_rate:>5.0%} {g.points:>+9.1f} "
            f"{g.points - base.points:>+8.1f} {g.avg_r:>+6.2f} {pf:>5} {g.net:>+10,.0f} "
            f"{o.max_drawdown_net:>10,.0f} {o.losing_streak:>6}"
        )
    return "\n".join(lines)
