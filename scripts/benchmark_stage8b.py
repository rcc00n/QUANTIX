#!/usr/bin/env python3
"""Read-only matched funding comparison over previously verified retained evidence.

Takes the causal cohort cut from quantix-full-v2 before planning changes. No
network, database writes, orders, fills, or P&L estimates are produced. Every
future observation is assessed against the fixed opening fund allocations.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import resource
import time
import zipfile
from collections import Counter
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from arbiq.opportunities.coverage import venue_group
from arbiq.paper_trading.allocation import Cell, describe_inventory, optimize_allocation
from arbiq.paper_trading.funding import (
    OPTIMIZED_POLICIES,
    ProposalRequest,
    baseline_comparison,
    constraints_for,
    optimized_strategy,
)
from arbiq.paper_trading.funding_plans import HistoricalRoute, funding_demands
from arbiq.paper_trading.funding_sizing import time_value
from arbiq.portfolio.contracts import decimal_context

D = Decimal
MAX_SAMPLE_BYTES = 128 * 1024 * 1024
MAX_SAMPLE_ROWS = 150


def historical_route(row):
    buy, sell = row["buy"], row["sell"]
    evaluation = SimpleNamespace(
        **{
            **row,
            "evaluated_at": time_value(row["evaluated_at"]),
            "created_at": time_value(row["created_at"]),
        }
    )
    return HistoricalRoute(
        row["route_id"],
        Cell(*buy[:2]),
        Cell(*sell[:2]),
        evaluation,
        row["frequency"],
        buy[2],
        sell[2],
        buy[3],
        sell[3],
        buy[4],
        sell[4],
    )


