"""stock-intel: portfolio_diff — material position changes between two holdings snapshots."""
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

MATERIAL_CHANGE_PCT = 0.25


class ChangeType(Enum):
    NEW = "new"
    EXIT = "exit"
    INCREASED = "increased"
    DECREASED = "decreased"


@dataclass(frozen=True)
class HoldingChange:
    ticker: str
    change_type: ChangeType
    prior_shares: float
    current_shares: float
    pct_change: float | None  # None for NEW positions


def diff_holdings(
    prior: Mapping[str, float],
    current: Mapping[str, float],
    material_change_pct: float = MATERIAL_CHANGE_PCT,
) -> list[HoldingChange]:
    """Material changes from prior to current (ticker -> shares), sorted by ticker."""
    if any(v < 0 for v in (*prior.values(), *current.values())):
        raise ValueError("share counts must be non-negative")
    changes: list[HoldingChange] = []
    for ticker in sorted(prior.keys() | current.keys()):
        old = prior.get(ticker, 0.0)
        new = current.get(ticker, 0.0)
        if old == 0 and new == 0:
            continue
        if old == 0:
            changes.append(HoldingChange(ticker, ChangeType.NEW, old, new, None))
        elif new == 0:
            changes.append(HoldingChange(ticker, ChangeType.EXIT, old, new, -1.0))
        else:
            pct = (new - old) / old
            if abs(pct) >= material_change_pct or math.isclose(abs(pct), material_change_pct):
                kind = ChangeType.INCREASED if pct > 0 else ChangeType.DECREASED
                changes.append(HoldingChange(ticker, kind, old, new, pct))
    return changes
