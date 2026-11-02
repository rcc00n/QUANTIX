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


def market_summary(cohort, minimum):
    return {
        "priced_size_points": len(cohort.demands),
        "routes_with_priced_evidence": len({d.route_id for d in cohort.demands}),
        "routes_with_any_positive_meaningful_projection": len(
            {d.route_id for d in cohort.demands if d.profitable and d.acquisition_cad >= minimum}
        ),
        "exclusions": dict(cohort.exclusions),
    }


@decimal_context
async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", type=D, default=D(2500))
    parser.add_argument("--max-reserve-pct", type=D, default=D(50))
    args = parser.parse_args()
    started = time.process_time()
    metadata = json.loads(args.cohort.read_text())
    training, later = cohort_routes(args.sample, metadata)
    cutoff, finish = time_value(metadata["cutoff"]), time_value(metadata["forward_through"])
    snapshot, exchanges, assets, reserve_venues, cash_exchange, stablecoins, groups = (
        archive_metadata(args.archive, cutoff)
    )
    if snapshot != metadata["archive_snapshot_at"]:
        raise ValueError("Archive snapshot and sample metadata disagree")
    archive_preparation_cpu_ms = (time.process_time() - started) * 1000
    sizing_start = time.process_time()
    common = {"trade_amount_cad": args.target, "max_reserve_pct": args.max_reserve_pct}
    representative = ProposalRequest(**common)
    strategy = optimized_strategy(representative)
    prior = funding_demands(training, cutoff, strategy, include_legacy=True)
    future = funding_demands(later, finish, strategy)
    sizing_cpu_ms = (time.process_time() - sizing_start) * 1000
    records = []
    by_capital = {}
    for capital in (10000, 50000, 100000):
        body = ProposalRequest(capital_cad=D(capital), **common)
        constraints = constraints_for(body)
        by_capital[str(capital)] = []
        for policy in ("COVERAGE", *OPTIMIZED_POLICIES):
            sys_start = time.process_time()
            if policy == "COVERAGE":
                comparison = await baseline_comparison(
                    body,
                    prior,
                    constraints,
                    exchanges,
                    assets,
                    cash_exchange,
                    groups,
                    stablecoins,
                    cutoff,
                )
            else:
                comparison = optimize_allocation(
                    policy,
                    constraints,
                    prior.demands,
                    reserve_venues,
                    stablecoins,
                    venue_groups=groups,
                    as_of=cutoff,
                ).as_payload()
            allocation_cpu_ms = (time.process_time() - sys_start) * 1000
            fixed = frozen_inventory(comparison["allocations"])
            assessment_start = time.process_time()
            forward = describe_inventory(
                constraints,
                future.demands,
                fixed,
                venue_groups=groups,
                stablecoin_assets=stablecoins,
                as_of=finish,
            )
            used = D(comparison["total_cad"])
            unplaced = D(comparison["unallocated_cad"])
            if used + unplaced != capital:
                raise ArithmeticError("Initial accounting capital did not reconcile")
            summary = comparison["allocation_summary"]
            exchange_values = {k: row["cad_value"] for k, row in summary["by_exchange"].items()}
            record = {
                "capital_cad": str(capital),
                "policy": policy,
                "initial_placed_cad": str(used),
                "unplaced_cad": str(unplaced),
                "feasible": comparison["feasible"],
                "reasons": comparison["reasons"],
                "reserve_cad": summary["reserve_cad"],
                "invested_cad": summary["invested_cad"],
                "stablecoin_cad": summary["stablecoin_cad"],
                "funded_cells": len(fixed),
                "highest_exchange_share_pct": str(
                    max((D(x["share_pct"]) for x in summary["by_exchange"].values()), default=D(0))
                ),
                "highest_asset_share_pct": str(
                    max((D(x["share_pct"]) for x in summary["by_asset"].values()), default=D(0))
                ),
                "venue_values_cad": exchange_values,
                "training": assessment_summary(comparison),
                "first_matching_forward": assessment_summary(forward),
                "allocation_cpu_ms": allocation_cpu_ms,
                "followup_assessment_cpu_ms": (time.process_time() - assessment_start) * 1000,
            }
            if policy == "COVERAGE":
                record["legacy_nominal_individual_coverage"] = comparison[
                    "legacy_individual_coverage"
                ]
            records.append(record)
            by_capital[str(capital)].append(
                {
                    "policy": policy,
                    "feasible": record["feasible"],
                    "training_targets": comparison["funded_route_count"],
                    "held_forward_targets": forward["funded_route_count"],
                }
            )
    with args.sample.open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    payload = {
        "kind": "PRESERVED_PRODUCTION_RETAINED_EVIDENCE_PLANNING_COMPARISON",
        "cohort": metadata,
        "sample_sha256": fingerprint,
        "shared_controls": representative.model_dump(mode="json"),
        "first_forward_routes_available": len(later),
        "planning_market": market_summary(prior, strategy.min_buy_capital_cad),
        "forward_market": market_summary(future, strategy.min_buy_capital_cad),
        "reserve_venues_with_cad_at_cutoff": reserve_venues,
        "archive_preparation_cpu_ms": archive_preparation_cpu_ms,
        "shared_sizing_cpu_ms": sizing_cpu_ms,
        "benchmark_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "actual_forward_session_counts": None,
        "realized_simulated_pnl_cad": None,
        "unrealized_simulated_pnl_cad": None,
        "holdings_benchmark_return": None,
        "interpretation": [
            "First retained event per route. Missing evidence stays missing.",
            "The training and forward slices only cover retained observations.",
            "Forward inventory is held fixed and reserve is excluded from planning coverage.",
            "Static coverage has no orders, fills, rejection rates or P&L.",
            "Baseline uses the same evidence and the old sizing that maximizes net within a cap.",
            "Infeasible plans show their funded subsets. Unplaced CAD cannot fund a session.",
        ],
        "records": records,
    }
    descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(payload, target, indent=2)
        target.write("\n")
    print(
        json.dumps({"records": len(records), "counts": by_capital, "sizing_cpu_ms": sizing_cpu_ms})
    )


if __name__ == "__main__":
    asyncio.run(main())
