"""Synthetic profile contracts and evidence admissions, never live fills."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from arbiq.paper_trading.coordinator import PaperCoordinator
from arbiq.paper_trading.execution import recheck_projection
from arbiq.paper_trading.funding_sizing import read_pair
from arbiq.settings.strategy import (
    PaperStrategy,
    read_pinned_strategy,
    strategy_lab_settings,
)
from pydantic import ValidationError
from test_paper_execution import book, inputs
from test_stage8b_planner import AT, D, pinned


def test_new_profiles_require_isolated_healthy_prefunded_research():
    legacy = read_pinned_strategy({})
    assert legacy.strategy_lab_profile is None
    assert "strategy_lab_profile" not in legacy.model_dump(mode="json")
    for profile in (
        "LIQUID_ARBITRAGE",
        "ANOMALY_HUNTER",
        "HYBRID_OPTIMIZER",
        "NO_TRADING",
    ):
        strategy = strategy_lab_settings(profile)
        settings = strategy.model_dump(mode="json")
        assert settings["strategy_lab_profile"] == profile
        assert strategy == read_pinned_strategy(settings)
        assert strategy.mode == "RESEARCH_SHADOW"
        assert strategy.research_execution_model == "PRE_FUNDED"
        for change in (
            {"research_execution_model": "TRANSFER"},
            {"allow_missing_source_timestamp": True},
            {"minimum_confidence": "ANOMALY_RESEARCH"},
            {"minimum_profit_cad": "0"},
            {"threshold_logic": "OR"},
        ):
            with pytest.raises(ValidationError):
                PaperStrategy.model_validate({**settings, **change})


def test_anomaly_requires_independent_resolution_not_merely_an_ordinary_recheck():
    evaluation = pinned()
    evaluation.evidence["both_legs_rechecked"] = True
    evaluation.evidence["buy_book"]["verification"].update(
        status="CONFIRMED",
        fresh_rest_recheck=True,
    )
    anomaly = strategy_lab_settings("ANOMALY_HUNTER")
    assert read_pair(evaluation, AT, strategy_lab_settings("LIQUID_ARBITRAGE"))
    with pytest.raises(ValueError, match="CONFIRMED_ANOMALY_EVIDENCE_UNAVAILABLE"):
        read_pair(evaluation, AT, anomaly)
    evaluation.evidence["buy_book"]["verification"]["resolved_reference_anomaly"] = True
    assert read_pair(evaluation, AT, anomaly)
    assert read_pair(evaluation, AT, strategy_lab_settings("HYBRID_OPTIMIZER"))
    with pytest.raises(ValueError, match="OBSERVATION_BELONGS_TO_ANOMALY_COHORT"):
        read_pair(evaluation, AT, strategy_lab_settings("LIQUID_ARBITRAGE"))
    evaluation.evidence["buy_book"]["state"] = "DEGRADED"
    with pytest.raises(ValueError, match="STRATEGY_LAB_REQUIRES_HEALTHY_SOURCE_BOOKS"):
        read_pair(evaluation, AT, anomaly)


@pytest.mark.asyncio
async def test_monitor_only_profile_never_starts_a_candidate_or_ledger_transaction():
    control = SimpleNamespace(settings=strategy_lab_settings("NO_TRADING").model_dump(mode="json"))
    await PaperCoordinator.schedule(None, None, control, None, None, None, AT)


def test_delayed_recheck_refuses_cross_book_source_skew_with_uncertainty():
    buy = book(uuid4(), "synthetic-buy", AT - timedelta(seconds=9))
    sell = book(uuid4(), "synthetic-sell", AT, bid="110", ask="111")
    with pytest.raises(ValueError, match="STRATEGY_LAB_CROSS_BOOK_SKEW"):
        recheck_projection(
            buy_book=buy,
            sell_book=sell,
            buy_input=inputs(buy, "synthetic-buy"),
            sell_input=inputs(sell, "synthetic-sell"),
            planned_quantity=D(5),
            decision_at=AT,
            strategy=strategy_lab_settings("HYBRID_OPTIMIZER"),
        )
