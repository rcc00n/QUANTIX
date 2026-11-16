# ruff: noqa: F811
"""Explicitly SYNTHETIC retained-data exports. Never writes live application records."""

import asyncio
import csv
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from arbiq.analytics.export_jobs import ExportJobs
from arbiq.analytics.full_export import (
    EXCLUDED_TABLES,
    TABLE_GROUP,
    FullArchive,
    dataset_query,
    public_record,
)
from arbiq.db.base import Base
from arbiq.db.models import PaperSession, User
from test_paper_api import private_api  # noqa: F401


def test_explicit_schema_policy_and_private_projection():
    assert set(Base.metadata.tables) == TABLE_GROUP.keys() | EXCLUDED_TABLES.keys()
    owner = uuid4()
    columns, query, scoped = dataset_query(
        Base.metadata.tables["notification_outbox"], owner
    )
    names = {c.name for c in columns}
    assert scoped and not names & {
        "message",
        "binding_id",
        "user_id",
        "provider_message_id",
    }
    assert "WHERE" in str(query)
    for name in (
        "paper_fills",
        "ledger_entries",
        "paper_controls",
        "portfolio_positions",
        "notification_events",
    ):
        assert dataset_query(Base.metadata.tables[name], owner)[2], name
    assert not dataset_query(Base.metadata.tables["arbitrage_evaluations"], owner)[2]








