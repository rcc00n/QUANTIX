"""Explicitly SYNTHETIC records; never insert fixture fills into application data."""

# Pytest discovers the explicitly imported shared synthetic fixtures by name.
# ruff: noqa: F811

import asyncio
import csv
import hashlib
import io
import itertools
import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from arbiq.analytics.contracts import CSV_COLUMNS, EXPORT_FILES, ReportPeriod
from arbiq.analytics.export import Archive, cell, make_export, sanitize
from arbiq.analytics.service import period_for
from arbiq.db.models import PaperSession, PaperTrade, StrategyVersion, User
from arbiq.paper_trading.models import PaperControl
from arbiq.portfolio.contracts import PriceEvidence
from arbiq.portfolio.models import PortfolioCheckpoint, PortfolioPosition
from arbiq.portfolio.valuation import snapshot
from fastapi import HTTPException
from sqlalchemy import select
from test_paper_api import private_api  # noqa: F401
from test_paper_execution import NOW, advance, at, book, environment  # noqa: F401


def rows(archive, name):
    return list(csv.DictReader(io.StringIO(archive.read(name).decode("utf-8"))))


def test_exact_csv_and_sensitive_metadata_redaction():
    assert cell(Decimal("-0.000000000000000123")) == "-0.000000000000000123"
    assert cell("-12.50") == "-12.50"
    assert cell("0E-18", financial=True) == "0.000000000000000000"
    with pytest.raises(ValueError):
        cell(0.1, financial=True)
    assert cell('=HYPERLINK("evil")').startswith("'=")
    assert cell("\tSUM(1)").startswith("'\t")
    value = sanitize(
        {
            "api_key": "SYNTHETIC-SECRET",
            "nested": {"authorization": "SYNTH"},
            "session_id": "join-me",
            "url": "https://synthetic/?token=PRIVATE&x=1",
        }
    )
    assert "SYNTHETIC-SECRET" not in json.dumps(value)
    assert "PRIVATE" not in value["url"] and value["session_id"] == "join-me"
    value = sanitize(
        {
            "apiKey": "SYNTHETIC",
            "accessToken": "PRIVATE",
            "token_address": "public-contract-identity",
            "databaseUrl": "postgresql://owner:PRIVATE@synthetic/db",
        }
    )
    assert value["apiKey"] == value["accessToken"] == "[REDACTED]"
    assert "PRIVATE" not in value["databaseUrl"]
    assert value["token_address"] == "public-contract-identity"
























