"""Single, authenticated, recoverable disk-backed export job; no HTTP generation timeout."""

import asyncio
import fcntl
import json
import os
import shutil
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from arbiq.analytics.api import _export_gate, file_hash
from arbiq.analytics.export import MAX_GENERATION_SECONDS, make_export, sequential_thread
from arbiq.analytics.export_budget import MAX_ZIP_BYTES, ExportCancelled, verify_archive
from arbiq.analytics.full_export import (
    MIN_FREE_BYTES,
    SCHEMA,
    TABLE_GROUP,
    build_full_export,
)
from arbiq.analytics.service import owned_session, period_for
from arbiq.auth.dependencies import AuthenticatedUser
from arbiq.db.models import ReportExport, SystemEvent
from arbiq.market_data.api import utc_reference

router = APIRouter(tags=["complete retained-data exports"])
ACTIVE = {"queued", "running"}
MAX_SECONDS = 2 * 60 * 60
TTL_SECONDS = 24 * 60 * 60


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["ALL_RETAINED", "CUSTOM"] = "ALL_RETAINED"
    session_id: UUID | None = None
    start: datetime | None = None
    end: datetime | None = None
    strategy_version_id: UUID | None = None


