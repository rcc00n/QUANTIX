#!/usr/bin/env python3
"""Assess the Strategy Lab's opening allocations on a fixed retained holdout."""

import argparse
import hashlib
import json
import resource
import time
from decimal import Decimal
from pathlib import Path

from arbiq.paper_trading.allocation import describe_inventory, optimize_allocation
from arbiq.paper_trading.funding import (
    ProposalRequest,
    constraints_for,
    optimized_strategy,
)
from arbiq.paper_trading.funding_plans import funding_demands
from arbiq.paper_trading.funding_sizing import time_value
from arbiq.portfolio.contracts import decimal_context
from benchmark_stage8b import (
    archive_metadata,
    assessment_summary,
    cohort_routes,
    frozen_inventory,
    market_summary,
)

PROFILES = {
    "LIQUID_ARBITRAGE": "LIQUIDITY_WEIGHTED",
    "ANOMALY_HUNTER": "ROUTE_OPTIMIZED",
    "HYBRID_OPTIMIZER": "DIVERSIFIED_HYBRID",
    "NO_TRADING": "DIVERSIFIED_HYBRID",
}
CAPITALS = tuple(Decimal(x) for x in (10000, 50000, 100000))


def funded_positive_routes(forward, fixed, settings):
    eligible = set()
    for route in forward.demands:
        if (
            not route.profitable
            or route.acquisition_cad < settings.min_buy_capital_cad
            or route.acquisition_cad > settings.trade_amount_cad
            or (route.liquidity_cad or Decimal(0)) < settings.minimum_liquidity_cad
            or any(fixed[item.cell] < item.quantity for item in (route.buy, route.sell))
        ):
            continue
        thresholds = (
            route.net_profit_cad >= settings.minimum_profit_cad,
            route.net_profit_cad / route.acquisition_cad * 100
            >= settings.minimum_profit_pct,
        )
        gate = all if settings.threshold_logic == "AND" else any
        if gate(thresholds):
            eligible.add(route.route_id)
    return len(eligible)


@decimal_context
def main(args):
    started = time.process_time()
    metadata = json.loads(args.cohort.read_text())
    prior, holdout = cohort_routes(args.sample, metadata)
    cutoff, finish = (
        time_value(metadata["cutoff"]),
        time_value(metadata["forward_through"]),
    )
    snapshot, _, _, reserve_venues, _, stablecoins, groups = archive_metadata(
        args.archive, cutoff
    )
    if snapshot != metadata["archive_snapshot_at"]:
        raise ValueError("Archive snapshot and research cohort disagree")
    records = []
    hybrid_inventory = {}
    for profile, policy in PROFILES.items():
        example = ProposalRequest(
            strategy_lab_profile=profile, trade_amount_cad=args.target
        )
        settings = optimized_strategy(example)
        planning = funding_demands(prior, cutoff, settings)
        later = funding_demands(holdout, finish, settings)
        for capital in CAPITALS:
            body = example.model_copy(update={"capital_cad": capital, "policy": policy})
            constraints = constraints_for(body)
            plan = optimize_allocation(
                policy,
                constraints,
                planning.demands,
                reserve_venues,
                stablecoins,
                venue_groups=groups,
                as_of=cutoff,
            ).as_payload()
            opening = {
                (item["exchange"], item["asset"]): (item["quantity"], item["cad_value"])
                for item in plan["allocations"]
            }
            if profile == "HYBRID_OPTIMIZER":
                hybrid_inventory[capital] = opening
            placed, unplaced = (
                Decimal(plan["total_cad"]),
                Decimal(plan["unallocated_cad"]),
            )
            if placed + unplaced != capital:
                raise ArithmeticError(
                    "An independent opening contribution does not reconcile"
                )
            fixed = frozen_inventory(plan["allocations"])
            data = describe_inventory(
                constraints,
                later.demands,
                fixed,
                venue_groups=groups,
                stablecoin_assets=stablecoins,
                as_of=finish,
            )
            records.append(
                {
                    "strategy_lab_profile": profile,
                    "allocation_policy": policy,
                    "contribution_cad": str(capital),
                    "placed_cad": str(placed),
                    "unallocated_cad": str(unplaced),
                    "opening_feasible": plan["feasible"],
                    "opening_inventory_matches_hybrid": (
                        opening == hybrid_inventory[capital]
                        if profile == "NO_TRADING"
                        else None
                    ),
                    "opening_limitations": plan["reasons"],
                    "forward_modeled_coverage": assessment_summary(data),
                    "forward_positive_and_fundable_routes": (
                        funded_positive_routes(later, fixed, settings)
                        if plan["feasible"] and profile != "NO_TRADING"
                        else 0
                    ),
                    "forward_market": market_summary(
                        later, settings.min_buy_capital_cad
                    ),
                    "scope": "RETAINED_FIRST_FORWARD_FRAME; NO EXECUTION ATTEMPT",
                    "paired_fills": None,
                    "realized_pnl_cad": None,
                    "unrealized_pnl_cad": None,
                    "benchmark_return_pct": None,
                }
            )
    with args.sample.open("rb") as stream:
        sample_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "scope": "RETAINED_CAUSAL_PLANNING_AND_FUNDING_HOLDOUT",
        "sample_sha256": sample_hash,
        "archive_snapshot_at": snapshot,
        "planning_cutoff": cutoff.isoformat(),
        "forward_through": finish.isoformat(),
        "training_routes": len(prior),
        "first_forward_routes": len(holdout),
        "requested_trade_capital_cad": str(args.target),
        "cpu_ms": (time.process_time() - started) * 1000,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "records": records,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--target", type=Decimal, default=Decimal(2500))
    print(json.dumps(main(parser.parse_args()), indent=2))
