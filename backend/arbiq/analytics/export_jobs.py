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


def utc_now():
    return datetime.now(UTC).isoformat()


class ExportJobs:
    def __init__(self, app, root=None):
        self.app = app
        self.root = Path(root or Path(tempfile.gettempdir()) / "quantix-full-exports")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.task = None

    def save(self, job):
        target = self.root / f"{job['id']}.json"
        handle, name = tempfile.mkstemp(dir=self.root, prefix=".status-")
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(job, stream, separators=(",", ":"))
                stream.flush()
                if job["status"] != "running":
                    os.fsync(stream.fileno())
            os.replace(name, target)
        finally:
            Path(name).unlink(missing_ok=True)

    def load(self, job_id, owner):
        try:
            job = json.loads((self.root / f"{job_id}.json").read_text())
        except FileNotFoundError:
            raise HTTPException(404, "Export job not found") from None
        if job["owner"] != str(owner):
            raise HTTPException(404, "Export job not found")
        return job

    def public(self, job):
        result = {k: v for k, v in job.items() if k not in {"owner", "clock"}}
        if job["status"] == "completed" and job.get("bytes", 0) > MAX_ZIP_BYTES:
            result.update(
                status="failed",
                download_url=None,
                error="Previous archive exceeds the new512000000byte limit. Create a new export.",
            )
        return result

    def lock(self):
        descriptor = os.open(self.root / ".worker.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor

    def cleanup_and_recover(self):
        """Caller holds process-independent volume lock: no worker can be live."""
        for path in self.root.glob("*.json"):
            job = json.loads(path.read_text())
            if job["status"] in ACTIVE:
                job.update(
                    status="failed",
                    error="EXPORT_INTERRUPTED: restart detected; click Export Everything to retry",
                    finished_at=utc_now(),
                )
                self.save(job)
                (self.root / f"{job['id']}.zip").unlink(missing_ok=True)
                (self.root / f"{job['id']}.partial").unlink(missing_ok=True)
                (self.root / f"{job['id']}.evidence.sqlite").unlink(missing_ok=True)
                (self.root / f"{job['id']}.cancel").unlink(missing_ok=True)
            if time.time() - path.stat().st_mtime > TTL_SECONDS:
                (self.root / f"{job['id']}.zip").unlink(missing_ok=True)
                path.unlink(missing_ok=True)
        for path in self.root.glob(".status-*"):
            path.unlink(missing_ok=True)

    async def start(self, owner, clock, options=None):
        descriptor = self.lock()
        if descriptor is None:
            for path in self.root.glob("*.json"):
                job = json.loads(path.read_text())
                if job["owner"] == str(owner) and job["status"] in ACTIVE:
                    return self.public(job)
            raise HTTPException(
                429, "A complete export is already running", headers={"Retry-After": "5"}
            )
        gate_owned = False
        try:
            if _export_gate.locked():
                raise HTTPException(
                    429, "Another analytical export is running", headers={"Retry-After": "5"}
                )
            await _export_gate.acquire()
            gate_owned = True
            self.cleanup_and_recover()
            if shutil.disk_usage(self.root).free < MIN_FREE_BYTES:
                raise HTTPException(507, "Export cannot start:6GiB disk safety reserve required")
            job = {
                "id": str(uuid4()),
                "owner": str(owner),
                "status": "queued",
                "created_at": utc_now(),
                "schema_version": SCHEMA,
                "scope": "ALL_RETAINED",
                "clock": clock,
                "completed_datasets": 0,
                "total_datasets": len(TABLE_GROUP),
                "dataset": None,
                "dataset_rows": 0,
                "uncompressed_bytes": 0,
                "error": None,
                "download_url": None,
                "final_zip_limit_bytes": MAX_ZIP_BYTES,
            }
            if options and options.scope == "CUSTOM":
                job.update(
                    scope="CUSTOM",
                    options=options.model_dump(mode="json"),
                    schema_version="quantix-export-v1",
                    total_datasets=None,
                )
            self.save(job)
            self.task = asyncio.create_task(self.run(job, descriptor), name="quantix-full-export")
            descriptor = None  # The running task owns the lock until file publication/audit.
            gate_owned = False
            return self.public(job)
        finally:
            if gate_owned:
                _export_gate.release()
            if descriptor is not None:
                os.close(descriptor)

