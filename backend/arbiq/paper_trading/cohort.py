"""Atomic publication of an independently funded, common-inventory research cohort."""

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, model_validator
from sqlalchemy import func, select

from arbiq.auth.dependencies import DB, AuthenticatedUser
from arbiq.db.models import Asset, Exchange, PaperSession, SystemEvent, User
from arbiq.finance.contracts import Contract
from arbiq.market_data.quality import CapturedBook, safe_json
from arbiq.paper_trading import allocation
from arbiq.paper_trading.api import now_checked, owned
from arbiq.paper_trading.archive import PortfolioLifecycleError, archive_portfolio
from arbiq.paper_trading.cohort_market import cache_json, confirmed_before, live_opening
from arbiq.paper_trading.funding import PLANNING_LIMIT, proposal_marks_fresh
from arbiq.paper_trading.funding_sizing import time_value
from arbiq.paper_trading.service import Allocation, create_portfolio, session_dto
from arbiq.portfolio.contracts import AccountingError, PriceEvidence, decimal_context
from arbiq.portfolio.valuation import snapshot
from arbiq.settings.models import FundingExperiment, FundingProposal
from arbiq.settings.strategy import PaperStrategy, strategy_lab_settings

router = APIRouter(prefix="/paper/funding", tags=["paper strategy cohort"])
VERSION = "quantix-common-opening-v1"
CAPITAL = Decimal(10000)
TARGET = Decimal(250)
LIMIT = Decimal(500)
MIN_BUY = Decimal(50)
PROFILES = {
    "LIQUID_ARBITRAGE": "Liquid Arbitrage",
    "ANOMALY_HUNTER": "Anomaly Hunter",
    "HYBRID_OPTIMIZER": "Hybrid Optimizer",
    "NO_TRADING": "Hold Control — No trading",
}
CONSTRAINTS = allocation.AllocationConstraints(
    capital_cad=CAPITAL,
    target_trade_cad=TARGET,
    min_buy_capital_cad=MIN_BUY,
    min_cell_cad=Decimal(50),
    max_asset_pct=Decimal(70),
    max_reserve_pct=Decimal(50),
)


class CohortPlanRequest(Contract):
    preview_only: bool = True


class CohortRequest(Contract):
    proposal_id: UUID
    experiment_id: UUID
    archive_session_ids: tuple[UUID, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def distinct_sessions(self):
        if len(set(self.archive_session_ids)) != len(self.archive_session_ids):
            raise ValueError("Archive portfolio IDs must be distinct")
        return self


def cohort_strategy():
    return strategy_lab_settings("HYBRID_OPTIMIZER").model_copy(
        update={
            "trade_amount_cad": LIMIT,
            "target_trade_cad": TARGET,
            "min_buy_capital_cad": MIN_BUY,
            "starting_capital_cad": CAPITAL,
        }
    )


async def plan_payload(request, db):
    """Read-only planning entry point shared by preview and immutable proposal creation."""
    strategy = cohort_strategy()
    now, run_id, eligible, sampled, demands, route_evidence, books, exclusions = await live_opening(
        request, db, strategy
    )
    groups = {row.exchange.slug: row.venue for row in eligible}
    reserve_venues = sorted(
        {row.exchange.slug for row in eligible if row.quote.canonical_key == allocation.CAD}
    )
    plan = await asyncio.to_thread(
        allocation.optimize_allocation,
        "DIVERSIFIED_HYBRID",
        CONSTRAINTS,
        demands,
        reserve_venues,
        {"asset:USDC", "asset:USDT"},
        venue_groups=groups,
        as_of=now,
    )
    payload = plan.as_payload()
    funded_routes = [
        {
            "route_id": item["route_id"],
            "base_asset": route_evidence[item["route_id"]]["base_asset"],
            "buy_exchange": item["target_pinned"]["buy"]["exchange"],
            "sell_exchange": item["target_pinned"]["sell"]["exchange"],
            "maximum_funded_acquisition_cad": item["maximum_funded_acquisition_cad"],
            "net_profit_cad": item["projected_net_profit_cad"],
        }
        for item in payload["route_capacity"]
        if item["target_pinned"]["target_met"]
        and "INSUFFICIENT_PRE_FUNDED_INVENTORY" not in item["limiting_constraints"]
    ]
    funded_assets = {route["base_asset"] for route in funded_routes}
    if len(funded_assets) < 2:
        payload["reasons"].append("MULTIASSET_TARGET_FUNDING_UNAVAILABLE")
        payload["feasible"] = False
    ready = payload["feasible"] and proposal_marks_fresh(payload, now)
    used_routes = {
        item["evidence"]["route_id"]
        for item in payload["allocations"]
        if item["evidence"].get("route_id")
    } | {item["route_id"] for item in funded_routes}
    required_ids = {
        key
        for route_id in used_routes
        for key in (
            route_evidence[route_id]["buy_market_id"],
            route_evidence[route_id]["sell_market_id"],
            *route_evidence[route_id]["fx_market_ids"],
        )
    }
    strategy = strategy.model_copy(
        update={
            "allowed_assets": tuple(sorted(funded_assets)),
            "allowed_exchanges": tuple(
                sorted(
                    {
                        slug
                        for route in funded_routes
                        for slug in (route["buy_exchange"], route["sell_exchange"])
                    }
                )
            ),
        }
    )
    return {
        **payload,
        "cohort_plan_version": VERSION,
        "classification": "RESEARCH",
        "as_of": now.isoformat(),
        "expires_at": (now + timedelta(seconds=30)).isoformat(),
        "creatable": ready,
        "maximum_trade_cad": str(LIMIT),
        "funded_routes": funded_routes,
        "funded_base_assets": sorted(funded_assets),
        "collector_run_id": run_id,
        "monitored_markets_in_scope": len(eligible),
        "sampled_monitored_books": len(sampled),
        "evidence_exclusions": exclusions,
        "live_opening_books": {key: book for key, book in books.items() if key in required_ids},
        "live_opening_route_evidence": {key: route_evidence[key] for key in sorted(used_routes)},
        "proposed_strategy": strategy.model_dump(mode="json"),
        "limitations": [
            "Current public depth establishes paper inventory capacity only",
            "Ordinary trade admission independently requires live positive net evidence",
            "Account eligibility and exact account fees remain unverified",
        ],
    }


@router.post("/cohort-plan")
async def cohort_plan(body: CohortPlanRequest, request: Request, db: DB, user: AuthenticatedUser):
    if PLANNING_LIMIT.locked():
        raise HTTPException(429, "Another bounded capital plan is running")
    async with PLANNING_LIMIT:
        payload = await plan_payload(request, db)
        if body.preview_only or not payload["creatable"]:
            return {"id": None, **payload}
        await db.scalar(select(User).where(User.id == user.id).with_for_update())
        now = await now_checked(request)
        if not proposal_marks_fresh(payload, now):
            raise HTTPException(409, "Live funding evidence expired before proposal publication")
        recent = await db.scalar(
            select(func.count())
            .select_from(FundingProposal)
            .where(
                FundingProposal.user_id == user.id,
                FundingProposal.created_at >= now - timedelta(hours=1),
            )
        )
        if recent >= 30:
            raise HTTPException(429, "Funding proposal limit reached; at most30 per hour")
        row = FundingProposal(
            user_id=user.id, expires_at=time_value(payload["expires_at"]), payload=payload
        )
        db.add(row)
        await db.commit()
        return {"id": str(row.id), **payload}


def experiment_key(experiment_id, profile):
    return f"cohort-v1:{experiment_id}:{profile}"


def request_fingerprint(body):
    value = {
        "proposal_id": str(body.proposal_id),
        "experiment_id": str(body.experiment_id),
        "archive_session_ids": sorted(map(str, body.archive_session_ids)),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


async def prior_result(db, user, body, fingerprint):
    keys = [experiment_key(body.experiment_id, profile) for profile in PROFILES]
    rows = {
        row.idempotency_key: row
        for row in await db.scalars(
            select(FundingExperiment).where(
                FundingExperiment.user_id == user.id, FundingExperiment.idempotency_key.in_(keys)
            )
        )
    }
    if not rows:
        return None
    if len(rows) != len(PROFILES) or any(row.fingerprint != fingerprint for row in rows.values()):
        raise HTTPException(409, "Experiment ID belongs to another cohort request or is incomplete")
    sessions = [session_dto(*(await owned(db, user, rows[key].session_id))) for key in keys]
    return {
        "experiment_id": str(body.experiment_id),
        "started_at": sessions[0]["lab_cohort"]["started_at"],
        "sessions": sessions,
        "archive_session_ids": sorted(map(str, body.archive_session_ids)),
        "reused": True,
    }


async def validate_live_proposal(request, proposal, strategy):
    """Keep the pinned contribution rates if all source books remain healthy and fresh."""
    client = request.app.state.collector_http
    status = await cache_json(client, "/feeds/status") if client is not None else None
    books = proposal.payload["live_opening_books"]
    if (
        not status
        or not status.get("healthy")
        or not status.get("clock", {}).get("trusted")
        or status.get("run_id") != proposal.payload["collector_run_id"]
        or not 0 < len(books) <= 48
    ):
        raise HTTPException(409, "Opening evidence no longer belongs to the live collector run")
    current = []
    keys = tuple(books)
    for offset in range(0, len(keys), 6):
        current.extend(
            await asyncio.gather(
                *(cache_json(client, f"/books/{key}") for key in keys[offset : offset + 6])
            )
        )
    now = await now_checked(request)
    if proposal.expires_at < now or not proposal_marks_fresh(proposal.payload, now):
        raise HTTPException(409, "Fresh opening evidence expired; request a new proposal")
    try:
        for key, data in zip(keys, current, strict=True):
            pinned, live = books[key], CapturedBook.model_validate(data)
            feed = status["items"][pinned["exchange"]]
            if (
                str(live.market_id) != key
                or live.exchange != pinned["exchange"]
                or live.base_key != pinned["base_key"]
                or live.quote_key != pinned["quote_key"]
                or feed.get("error_code")
                or live.symbol not in feed["active_monitored_markets"]
                or live.received_at < time_value(pinned["received_at"])
            ):
                raise ValueError("OPENING_BOOK_IDENTITY_OR_MONITORING_CHANGED")
            confirmed_before(live, now, strategy)
            confirmed_before(CapturedBook.model_validate(pinned), now, strategy)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(
            409, "Required opening books are no longer healthy and monitored"
        ) from None
    return now


def cohort_metadata(body, payload, now):
    return {
        "id": str(body.experiment_id),
        "proposal_id": str(body.proposal_id),
        "started_at": now.isoformat(),
        "allocation": {
            "allocations": [
                {
                    key: item[key]
                    for key in ("exchange", "asset", "quantity", "cad_rate", "cad_value")
                }
                for item in payload["allocations"]
            ],
            "reserve_cad": payload["allocation_summary"]["reserve_cad"],
            "unallocated_cad": payload["unallocated_cad"],
            "target_trade_cad": str(TARGET),
            "coverage_by_size": [
                item
                for item in payload["coverage_by_size"]
                if Decimal(item["trade_amount_cad"]) <= LIMIT
            ],
            "funded_routes": payload["funded_routes"],
        },
    }


async def opening_prices(db, allocations):
    exchange_ids = {
        row.slug: row.id
        for row in await db.scalars(
            select(Exchange).where(Exchange.slug.in_({item.exchange for item in allocations}))
        )
    }
    asset_ids = {
        row.canonical_key: row.id
        for row in await db.scalars(
            select(Asset).where(Asset.canonical_key.in_({item.asset for item in allocations}))
        )
    }
    return {
        (exchange_ids[item.exchange], asset_ids[item.asset]): PriceEvidence(
            item.cad_rate,
            time_value(item.evidence["observed_at"]),
            time_value(item.evidence["received_at"]),
            False,
            item.evidence["source"],
            item.evidence,
        )
        for item in allocations
        if item.asset != allocation.CAD
    }


@router.post("/cohorts", status_code=201)
@decimal_context
async def create_cohort(body: CohortRequest, request: Request, db: DB, user: AuthenticatedUser):
    await db.scalar(select(User).where(User.id == user.id).with_for_update())
    fingerprint = request_fingerprint(body)
    if prior := await prior_result(db, user, body, fingerprint):
        return prior
    proposal = await db.scalar(
        select(FundingProposal).where(
            FundingProposal.user_id == user.id, FundingProposal.id == body.proposal_id
        )
    )
    if proposal is None:
        raise HTTPException(404, "Common opening proposal not found")
    already_used = await db.scalar(
        select(SystemEvent.id)
        .where(
            SystemEvent.actor_user_id == user.id,
            SystemEvent.event_type == "paper.cohort_activated",
            SystemEvent.details["proposal_id"].astext == str(body.proposal_id),
        )
        .limit(1)
    )
    if already_used is not None:
        raise HTTPException(409, "The common opening proposal was already used")
    payload = proposal.payload
    if (
        payload.get("cohort_plan_version") != VERSION
        or payload.get("creatable") is not True
        or payload.get("feasible") is not True
        or Decimal(payload["unallocated_cad"]) != 0
        or len(payload["funded_base_assets"]) < 2
    ):
        raise HTTPException(422, "Select a feasible fresh multiasset opening proposal")
    ordered_ids = sorted(body.archive_session_ids)
    current = set(
        await db.scalars(
            select(PaperSession.id).where(
                PaperSession.user_id == user.id, PaperSession.ended_at.is_(None)
            )
        )
    )
    if current != set(ordered_ids):
        raise HTTPException(
            409, "Archive selection must match all previous unarchived owner portfolios"
        )
    strategy = PaperStrategy.model_validate(payload["proposed_strategy"])
    now = await validate_live_proposal(request, proposal, strategy)
    metadata = cohort_metadata(body, payload, now)
    allocations = [
        Allocation(
            exchange=item["exchange"],
            asset=item["asset"],
            quantity=Decimal(item["quantity"]),
            cad_rate=Decimal(item["cad_rate"]),
            evidence={
                **item["evidence"],
                "common_opening_proposal_id": str(proposal.id),
                "common_experiment_id": str(body.experiment_id),
            },
        )
        for item in payload["allocations"]
    ]
    try:
        archived = []
        for session_id in ordered_ids:
            old, old_control, pending = await archive_portfolio(db, user.id, session_id, now)
            if pending:
                raise HTTPException(
                    409, {"reason": "PREVIOUS_PORTFOLIO_NOT_SETTLED", "pending": pending}
                )
            archived.append((old, old_control))
        prices = await opening_prices(db, allocations)
        created = []
        for profile, name in PROFILES.items():
            session, control = await create_portfolio(
                db,
                user.id,
                name,
                strategy.model_copy(update={"strategy_lab_profile": profile}),
                CAPITAL,
                allocations,
                now,
                observed_funding=True,
                lab_cohort=metadata,
                passive=profile == "NO_TRADING",
            )
            mark = await snapshot(
                db,
                session.id,
                prices,
                now,
                research_only=True,
                benchmark_prices=prices,
                maximum_age_seconds=strategy.maximum_book_age_seconds,
            )
            if (
                Decimal(mark["equity_cad"] or "-1") != CAPITAL
                or Decimal(mark["no_trading_benchmark_cad"] or "-1") != CAPITAL
            ):
                raise AccountingError("Opening funding and no-trading benchmark must reconcile")
            db.add(
                FundingExperiment(
                    user_id=user.id,
                    session_id=session.id,
                    idempotency_key=experiment_key(body.experiment_id, profile),
                    fingerprint=fingerprint,
                )
            )
            created.append((session, control))
        # Age can expire during a slow accounting transaction; none of its rows may commit then.
        final_time = await now_checked(request)
        if (
            final_time < now
            or proposal.expires_at < final_time
            or not proposal_marks_fresh(payload, final_time)
        ):
            raise HTTPException(409, "Opening evidence expired before the transaction could commit")
        for record in payload.get("live_opening_books", {}).values():
            try:
                confirmed_before(CapturedBook.model_validate(record), final_time, strategy)
            except ValueError:
                raise HTTPException(409, "Required route evidence expired before commit") from None
        # Publish one observation boundary after every independent contribution reconciles.
        metadata = {**metadata, "started_at": final_time.isoformat()}
        for session, control in archived:
            session.ended_at = control.updated_at = final_time
        for session, control in created:
            session.started_at = control.updated_at = final_time
            session.benchmark_snapshot = {**session.benchmark_snapshot, "lab_cohort": metadata}
        outputs = [session_dto(session, control) for session, control in created]
        db.add(
            SystemEvent(
                actor_user_id=user.id,
                event_type="paper.cohort_activated",
                severity="info",
                message="Four independently funded common-inventory paper portfolios published",
                details=safe_json(
                    {
                        "experiment_id": body.experiment_id,
                        "proposal_id": body.proposal_id,
                        "activated_at": final_time,
                        "archived_session_ids": ordered_ids,
                        "session_ids": [record["id"] for record in outputs],
                    }
                ),
            )
        )
        await db.commit()
    except (PortfolioLifecycleError, AccountingError, ValueError) as exc:
        await db.rollback()
        raise HTTPException(409, str(exc)) from None
    return {
        "experiment_id": str(body.experiment_id),
        "started_at": final_time.isoformat(),
        "sessions": outputs,
        "archive_session_ids": sorted(map(str, ordered_ids)),
        "reused": False,
    }
