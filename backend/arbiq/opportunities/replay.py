"""Deterministic retained-evidence replay; availability time prevents look-ahead."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from arbiq.finance.contracts import utc_timestamp
from arbiq.market_data.quality import safe_json
from arbiq.opportunities.analysis import load_costs, load_input
from arbiq.opportunities.contracts import Costs, MarketInput, OptimizationResult
from arbiq.opportunities.math import DEFAULT_COSTS, optimize_route


@dataclass(frozen=True, slots=True)
class ReplayBook:
    market: MarketInput
    observed_at: datetime
    received_at: datetime
    evidence_id: str
    synthetic: bool = False

    def __post_init__(self) -> None:
        utc_timestamp(self.observed_at)
        utc_timestamp(self.received_at)
        if not self.evidence_id:
            raise ValueError("Replay requires provenance")


