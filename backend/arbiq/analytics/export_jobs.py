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

    async def audit(self, job, status, manifest=None):
        async with self.app.state.sessions() as db:
            db.add(
                ReportExport(
                    user_id=UUID(job["owner"]),
                    schema_version=job["schema_version"],
                    period_start=datetime(1970, 1, 1, tzinfo=UTC),
                    period_end=datetime.now(UTC),
                    status=status,
                    content_sha256=job.get("sha256"),
                    provenance={
                        "job_id": job["id"],
                        "scope": job["scope"],
                        "status": status,
                        "bytes": job.get("bytes"),
                        "error": job.get("error"),
                        "dataset_rows": manifest.get("total_dataset_rows") if manifest else None,
                    },
                )
            )
            db.add(
                SystemEvent(
                    actor_user_id=UUID(job["owner"]),
                    event_type=f"report.full_export_{status}",
                    severity="error" if status == "failed" else "info",
                    message="Authenticated complete retained-data export",
                    details={"job_id": job["id"], "status": status},
                )
            )
            await db.commit()

    async def run(self, job, descriptor):
        partial = self.root / f"{job['id']}.partial"
        target = self.root / f"{job['id']}.zip"
        started, last_save = time.monotonic(), 0.0
        engine = None

        async def progress(dataset, rows, byte_count, completed=False, restart=False):
            nonlocal last_save
            if restart:
                job.update(completed_datasets=0, coverage_level="analysis_snapshot")
            job.update(
                dataset=dataset,
                dataset_rows=rows,
                uncompressed_bytes=byte_count,
                elapsed_seconds=round(time.monotonic() - started, 1),
            )
            if completed:
                job["completed_datasets"] += 1
            if completed or time.monotonic() - last_save > 1:
                await sequential_thread(self.save, job.copy())
                last_save = time.monotonic()

        try:
            job.update(status="running", started_at=utc_now())
            self.save(job)
            await self.audit(job, "running")
            handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(handle)
            # One dedicated DB connection; no auth/dashboard pool slot is retained.
            engine = create_async_engine(
                self.app.state.settings.database_url.get_secret_value(),
                pool_size=1,
                max_overflow=0,
                hide_parameters=True,
                connect_args={
                    "server_settings": {
                        "application_name": "quantix-full-export",
                        "work_mem": "2MB",
                    }
                },
            )
            async with asyncio.timeout(MAX_SECONDS):
                async with async_sessionmaker(engine)() as db:
                    await db.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                    await db.execute(text("SET LOCAL statement_timeout = '120s'"))
                    if job["scope"] == "CUSTOM":
                        options = ExportRequest.model_validate(job["options"])
                        session, control = await owned_session(
                            db, UUID(job["owner"]), options.session_id, options.strategy_version_id
                        )
                        now = datetime.fromisoformat(job["clock"]["reference_utc"])
                        period = period_for(session, options.start, options.end, now)
                        async with asyncio.timeout(MAX_GENERATION_SECONDS):
                            custom_path, manifest = await make_export(
                                db,
                                session,
                                control,
                                period,
                                now,
                                job["clock"],
                                options.strategy_version_id,
                            )
                        os.replace(custom_path, partial)
                    else:
                        manifest = await build_full_export(
                            db, UUID(job["owner"]), partial, progress, job["clock"]
                        )
            await sequential_thread(verify_archive, partial)
            job.update(
                sha256=await sequential_thread(file_hash, partial),
                bytes=partial.stat().st_size,
                file_count=len(manifest["files"]) + (2 if job["scope"] == "ALL_RETAINED" else 1),
                dataset_rows=manifest.get("total_dataset_rows"),
                portfolio_count=len(
                    manifest.get("portfolio_ids", [job.get("options", {}).get("session_id")])
                ),
                counts=manifest.get("counts"),
                coverage_level=manifest.get("coverage_level"),
                lossless_raw_history=manifest.get("lossless_raw_history"),
                raw_payload_reduced_rows=manifest.get("raw_payload_reduced_rows", 0),
                intermediate_disk_peak_bytes=manifest.get("intermediate_disk_peak_bytes"),
            )
            await self.audit(job, "completed", manifest)
            os.replace(partial, target)
            job.update(
                status="completed",
                finished_at=utc_now(),
                expires_at=(datetime.now(UTC) + timedelta(seconds=TTL_SECONDS)).isoformat(),
                download_url=f"/api/reports/exports/{job['id']}/download",
                elapsed_seconds=round(time.monotonic() - started, 2),
            )
            self.save(job)
        except BaseException as error:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            cancelled = (
                isinstance(error, ExportCancelled) or partial.with_suffix(".cancel").exists()
            )
            if cancelled:
                reason = "EXPORT_CANCELLED: owner cancelled this export"
            elif isinstance(error, asyncio.CancelledError):
                reason = "EXPORT_INTERRUPTED: service shutdown; retry the complete export"
            elif isinstance(error, TimeoutError):
                reason = "EXPORT_TIME_LIMIT: safety deadline reached; no partial data published"
            elif isinstance(error, HTTPException):
                reason = "EXPORT_REJECTED: " + str(error.detail)
            elif isinstance(error, RuntimeError) and str(error).split(":")[0] in {
                "DISK_RESERVE",
                "OUTPUT_SAFETY_LIMIT",
                "UNCLASSIFIED_SCHEMA",
                "ESSENTIAL_DATA_EXCEEDS_BUDGET",
            }:
                reason = str(error)
            else:
                reason = (
                    "EXPORT_FAILED: generation could not finish; no partial ZIP published ("
                    + type(error).__name__
                    + ")"
                )
            job.update(
                status="cancelled" if cancelled else "failed", error=reason, finished_at=utc_now()
            )
            self.save(job)
            with suppress(Exception):
                await self.audit(job, "cancelled" if cancelled else "failed")
        finally:
            partial.with_suffix(".evidence.sqlite").unlink(missing_ok=True)
            partial.with_suffix(".cancel").unlink(missing_ok=True)
            try:
                if engine is not None:
                    await engine.dispose()
            finally:
                _export_gate.release()
                os.close(descriptor)

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


def manager(request):
    if not hasattr(request.app.state, "export_jobs"):
        request.app.state.export_jobs = ExportJobs(request.app)
    return request.app.state.export_jobs


@router.post("/reports/exports", status_code=202)
async def create_export(request: Request, user: AuthenticatedUser, options: ExportRequest):
    owner = user.id
    if options.scope == "CUSTOM" and options.session_id is None:
        raise HTTPException(422, "Custom export requires a paper portfolio")
    if options.scope == "ALL_RETAINED" and any(
        (options.session_id, options.start, options.end, options.strategy_version_id)
    ):
        raise HTTPException(422, "Full export does not accept filters")
    now, clock = await utc_reference(request)
    return await manager(request).start(owner, {**clock, "reference_utc": now.isoformat()}, options)


