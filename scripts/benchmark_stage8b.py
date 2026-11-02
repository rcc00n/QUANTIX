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


def cohort_routes(path, metadata):
    if not 0 < path.stat().st_size <= MAX_SAMPLE_BYTES:
        raise ValueError("Private planning sample exceeds the benchmark input bound")
    cutoff = time_value(metadata["cutoff"])
    finish = time_value(metadata["forward_through"])
    training, later, seen = {}, {}, set()
    with path.open() as reader:
        for index, line in enumerate(reader, start=1):
            if index > MAX_SAMPLE_ROWS:
                raise ValueError("Planning sample exceeds the route evidence bound")
            row = json.loads(line)
            route = historical_route(row)
            evaluation = route.evaluation
            if evaluation.id in seen:
                raise ValueError("Duplicate retained evaluation")
            seen.add(evaluation.id)
            if row["split"] == "training":
                if (
                    route.route_id in training
                    or evaluation.evaluated_at > cutoff
                    or evaluation.created_at > cutoff
                ):
                    raise ValueError("Training record is unavailable at planning cutoff")
                training[route.route_id] = route
            elif row["split"] == "forward":
                if not cutoff < evaluation.evaluated_at <= evaluation.created_at <= finish:
                    raise ValueError("Forward record falls outside its separate time window")
                rank = evaluation.created_at, evaluation.evaluated_at, evaluation.id
                previous = later.get(route.route_id)
                if previous is None or rank < previous[0]:
                    later[route.route_id] = rank, route
            else:
                raise ValueError("Unknown evidence partition")
    if not training or set(later) - set(training):
        raise ValueError("Forward opportunities must refer to the frozen training route universe")
    if len(training) != metadata["training_sample"] or len(seen) != (
        metadata["training_sample"] + metadata["forward_sample"]
    ):
        raise ValueError("Causal sample disagrees with its recorded counts")
    # Freeze one first retained record per route before knowing whether it can be
    # priced or profitable. Missing evidence never triggers a favorable substitution.
    return list(training.values()), [data[1] for data in later.values()]


def archive_metadata(path, cutoff):
    def rows(z, dataset):
        matches = [name for name in z.namelist() if name.endswith(f"/{dataset}.csv")]
        if len(matches) != 1:
            raise ValueError(f"Expected one dataset named {dataset}")
        with z.open(matches[0]) as stream:
            yield from csv.DictReader(io.TextIOWrapper(stream, encoding="utf-8"))

    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if not manifest["complete"] or manifest["schema_version"] != "quantix-full-v2":
            raise ValueError("A complete quantix-full-v2 archive is required")
        exchanges = {
            UUID(x["id"]): SimpleNamespace(id=UUID(x["id"]), slug=x["slug"])
            for x in rows(archive, "exchanges")
            if time_value(x["created_at"]) <= cutoff and x["enabled"].lower() == "true"
        }
        assets = {
            UUID(x["id"]): SimpleNamespace(**x)
            for x in rows(archive, "assets")
            if time_value(x["created_at"]) <= cutoff
        }
        cad = str(
            next(identifier for identifier, x in assets.items() if x.canonical_key == "fiat:CAD")
        )
        reserve_venues = sorted(
            {
                exchanges[UUID(x["exchange_id"])].slug
                for x in rows(archive, "markets")
                if x["quote_asset_id"] == cad
                and UUID(x["exchange_id"]) in exchanges
                and time_value(x["created_at"]) <= cutoff
            }
        )
    cash_exchange = next(
        x
        for x in sorted(
            exchanges.values(), key=lambda x: (x.slug not in {"kraken", "ndax"}, x.slug)
        )
        if x.slug in reserve_venues
    )
    stablecoins = {x.canonical_key for x in assets.values() if x.kind == "stablecoin"}
    groups = {x.slug: venue_group(x.slug) for x in exchanges.values()}
    return (
        manifest["snapshot_at"],
        exchanges,
        assets,
        reserve_venues,
        cash_exchange,
        stablecoins,
        groups,
    )


@decimal_context
def frozen_inventory(allocations):
    quantities = Counter()
    for item in allocations:
        evidence = item.get("evidence", {})
        if not evidence.get("reserve") and not evidence.get("cash_remainder"):
            quantities[Cell(item["exchange"], item["asset"])] += D(item["quantity"])
    return quantities


def assessment_summary(data):
    return {
        key: data[key]
        for key in (
            "coverage_by_size",
            "funded_route_count",
            "simultaneous_funded_route_count",
            "median_funded_acquisition_cad",
        )
    }


