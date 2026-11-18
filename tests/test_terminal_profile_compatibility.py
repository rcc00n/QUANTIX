"""Synthetic persistence test for strict pre-profile notifier/rollback contracts."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from arbiq.analytics.full_export import dataset_query, public_record
from arbiq.collector.config import MarketProfile
from arbiq.db.models import SystemEvent, User
from arbiq.finance.contracts import Contract
from arbiq.settings.api import terminal_history
from arbiq.settings.models import TerminalSettingsVersion
from arbiq.settings.terminal import (
    SETTINGS_CHANGED_EVENT,
    MarketPreferences,
    NotificationPreferences,
    TerminalSettings,
    read_effective_terminal,
    save_terminal,
)
from pydantic import Field, create_model
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

LegacyMarketPreferences = create_model(
    "LegacyMarketPreferences",
    __base__=Contract,
    **{
        key: (field.annotation, field)
        for key, field in MarketPreferences.model_fields.items()
        if key != "market_profile"
    },
)
LegacyTerminalSettings = create_model(
    "LegacyTerminalSettings",
    __base__=Contract,
    schema_version=(
        TerminalSettings.model_fields["schema_version"].annotation,
        "terminal-v1",
    ),
    market=(LegacyMarketPreferences, Field(default_factory=LegacyMarketPreferences)),
    notifications=(
        NotificationPreferences,
        Field(default_factory=NotificationPreferences),
    ),
)


async def test_market_profile_survives_same_transaction_and_legacy_rollback(
    database_url,
):
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 1, 4, 16, 5, tzinfo=UTC)
    try:
        async with sessions() as db:
            owner, other = [
                User(
                    username=f"synthetic-terminal-{uuid4().hex}",
                    password_hash="SYNTHETIC",
                )
                for _ in range(2)
            ]
            db.add_all((owner, other))
            await db.flush()
            requested = TerminalSettings(
                market=MarketPreferences(
                    market_profile=MarketProfile.BALANCED_ADAPTIVE,
                    enabled_assets=("BTC", "SOL"),
                    maximum_age_seconds=42,
                ),
                notifications=NotificationPreferences(
                    telegram_enabled=True,
                    minimum_liquidity_cad=Decimal("53.009"),
                    quiet_start="22:10",
                ),
            )
            version = await save_terminal(db, owner.id, requested, now, 0)
            legacy = LegacyTerminalSettings.model_validate(version.settings)
            assert legacy.model_dump(mode="json") == requested.model_dump(
                mode="json", exclude={"market": {"market_profile"}}
            )
            assert "market_profile" not in version.settings["market"]
            db.add(
                SystemEvent(
                    actor_user_id=other.id,
                    event_type=SETTINGS_CHANGED_EVENT,
                    severity="info",
                    message="SYNTHETIC wrong owner's profile event",
                    details={
                        "settings_version_id": str(version.id),
                        "market_profile": "LIQUID_MARKETS",
                    },
                    created_at=now + timedelta(days=10_000),
                )
            )
            effective, settings = await read_effective_terminal(db, owner.id)
            assert effective.id == version.id and settings == requested
            history = await terminal_history(db, owner)
            assert history["items"][0]["settings"] == requested.model_dump(mode="json")

            _, event_query, scoped = dataset_query(SystemEvent.__table__, owner.id)
            events = (
                (
                    await db.execute(
                        event_query.where(
                            SystemEvent.event_type == SETTINGS_CHANGED_EVENT
                        )
                    )
                )
                .mappings()
                .all()
            )
            assert scoped and len(events) == 1
            public_event = public_record(dict(events[0]))
            assert "actor_user_id" not in public_event
            assert public_event["details"]["settings_version_id"] == str(version.id)
            assert public_event["details"]["market_profile"] == "BALANCED_ADAPTIVE"
            _, version_query, scoped = dataset_query(
                TerminalSettingsVersion.__table__, owner.id
            )
            exported_version = (await db.execute(version_query)).mappings().one()
            assert scoped and exported_version["id"] == version.id
            LegacyTerminalSettings.model_validate(exported_version["settings"])

            # The old app can save a later compatible row without an extension event.
            version_id = version.id
            async with db.begin_nested() as rollback:
                db.add(
                    TerminalSettingsVersion(
                        user_id=owner.id,
                        version=2,
                        settings=legacy.model_dump(mode="json"),
                        effective_at=now + timedelta(seconds=1),
                    )
                )
                latest, replacement = await read_effective_terminal(db, owner.id)
                assert latest.version == 2 and replacement.market.market_profile is None
                assert replacement.notifications == requested.notifications
                await rollback.rollback()
            restored, restored_preferences = await read_effective_terminal(db, owner.id)
            assert restored.id == version_id and restored_preferences == requested
    finally:
        await engine.dispose()
