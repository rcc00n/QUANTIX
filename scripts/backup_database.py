#!/usr/bin/env python3
# ruff: noqa: UP017 -- Local host and audited Ubuntu22.04 use Python3.10.
"""Private consistent pg_dump archive. No passwords on argv or in output metadata."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
PENDING_NAME = re.compile(r"\.quantix-backup-[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def sidecar(archive: Path) -> Path:
    return archive.with_name(archive.name + ".json")


def present(path: Path) -> bool:
    return path.is_symlink() or path.exists()


def private_regular(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & 0o077
    )


def private_directory(path: Path) -> bool:
    st = path.lstat()
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.geteuid()
        and not st.st_mode & 0o077
    )


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def backup_directory(path: Path):
    if not private_directory(path):
        raise ValueError(
            "Backup directory must be private and owned by the invoking user"
        )
    flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path / ".quantix-backup.lock", flags, 0o600)
    try:
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.geteuid()
            or st.st_mode & 0o077
        ):
            raise ValueError("Backup lock must be a private regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another backup operation owns this directory") from None
        yield
    finally:
        os.close(fd)


def archive_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        header = stream.read(5)
        if header != b"PGDMP":
            raise ValueError("Invalid PostgreSQL custom-format archive")
        digest.update(header)
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(path: Path, name: str, archive: Path) -> dict:
    if (
        not private_regular(path)
        or path.stat().st_size > 65536
        or not private_regular(archive)
    ):
        raise ValueError("Backup requires a private archive and bounded metadata")
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("kind") != "PRIVATE_POSTGRESQL_BACKUP"
        or data.get("archive") != name
        or type(data.get("bytes")) is not int
        or data["bytes"] != archive.stat().st_size
        or type(data.get("production")) is not bool
        or not isinstance(data.get("sha256"), str)
        or not SHA256.fullmatch(data["sha256"])
    ):
        raise ValueError("Backup metadata disagrees with the archive")
    return data


def verified_backup(path: Path) -> dict:
    data = manifest(sidecar(path), path.name, path)
    if archive_digest(path) != data["sha256"]:
        raise ValueError("Backup checksum verification failed")
    return data


def publish_link(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
    except FileExistsError:
        if not private_regular(target) or not source.samefile(target):
            raise ValueError(
                "Refusing to overwrite an existing backup artifact"
            ) from None


def publish(stage: Path, target: Path) -> None:
    # The metadata path is the commit marker. Lock-free readers can use it once
    # the archive link is durable, even if the process exits before cleanup.
    publish_link(stage / "archive", target)
    fsync_directory(target.parent)
    publish_link(stage / "manifest", sidecar(target))
    fsync_directory(target.parent)


def clear_stage(stage: Path) -> None:
    for name in ("archive", "manifest"):
        (stage / name).unlink(missing_ok=True)
    stage.rmdir()


def recover_stages(directory: Path) -> None:
    for stage in directory.glob(".quantix-backup-*"):
        if not PENDING_NAME.fullmatch(stage.name) or not private_directory(stage):
            continue
        if any(
            p.name not in {"archive", "manifest"} or not private_regular(p)
            for p in stage.iterdir()
        ):
            continue
        meta = stage / "manifest"
        archive = stage / "archive"
        # A missing or truncated manifest predates either publication link.
        try:
            if not private_regular(meta) or meta.stat().st_size > 65536:
                raise ValueError("Incomplete staging metadata")
            name = json.loads(meta.read_text())["archive"]
            if (
                not isinstance(name, str)
                or name != Path(name).name
                or name.startswith(".")
            ):
                raise ValueError("Invalid archive name")
            data = manifest(meta, name, archive)
        except (ValueError, TypeError, KeyError):
            clear_stage(stage)
            continue
        if archive_digest(archive) != data["sha256"]:
            raise ValueError("An interrupted backup has a checksum mismatch")
        publish(stage, directory / name)
        clear_stage(stage)


def create_pair(target: Path, archive_writer, metadata: dict) -> dict:
    stage = target.parent / (".quantix-backup-" + uuid4().hex)
    stage.mkdir(mode=0o700)
    prepared = False
    try:
        archive = stage / "archive"
        with os.fdopen(
            os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb"
        ) as stream:
            archive_writer(stream)
            stream.flush()
            os.fsync(stream.fileno())
        digest = archive_digest(archive)
        size = archive.stat().st_size
        if (
            metadata.get("sha256", digest) != digest
            or metadata.get("bytes", size) != size
        ):
            raise ValueError("Replica checksum or size mismatch")
        data = {**metadata, "archive": target.name, "sha256": digest, "bytes": size}
        with os.fdopen(
            os.open(stage / "manifest", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
            "w",
        ) as stream:
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        fsync_directory(stage)
        fsync_directory(target.parent)
        prepared = True
        publish(stage, target)
    except BaseException:
        if not prepared:
            clear_stage(stage)
        raise
    clear_stage(stage)
    return data


def rotate_backups(directory: Path, keep: int):
    """Rotate only self-identified duplicate recovery archives, never application data."""
    archives = []
    for meta in directory.glob("quantix-*.dump.json"):
        archive = meta.with_suffix("")
        if not re.fullmatch(r"quantix-\d{8}T\d{6}Z\.dump", archive.name):
            continue
        if any(
            p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077
            for p in (meta, archive)
        ):
            continue
        try:
            value = json.loads(meta.read_text())
            valid = (
                value.get("kind") == "PRIVATE_POSTGRESQL_BACKUP"
                and value.get("production") is True
                and value.get("archive") == archive.name
                and value.get("bytes") == archive.stat().st_size
                and re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))
            )
        except (OSError, ValueError, TypeError):
            continue
        if valid:
            archives.append((archive, meta))
    for archive, meta in sorted(archives, key=lambda pair: pair[0].name, reverse=True)[
        keep:
    ]:
        archive.unlink()
        meta.unlink()


def dump_archive(stream, *, production: bool) -> None:
    compose = ["docker", "compose"]
    if production:
        compose += ["-f", "infra/compose.production.yaml"]
    process = subprocess.run(
        compose
        + [
            "exec",
            "-T",
            "db",
            "sh",
            "-c",
            'exec pg_dump -Fc --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
        ],
        cwd=ROOT,
        stdout=stream,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    if process.returncode:
        raise ValueError("Database backup failed before publication")


def replicate(source: Path, directory: Path) -> dict:
    """Copy an already committed backup into an explicitly provisioned mount."""
    with backup_directory(directory):
        target = directory / source.name
        if source.parent.samefile(directory):
            raise ValueError(
                "Replica destination must differ from the source directory"
            )
        recover_stages(directory)
        data = manifest(sidecar(source), source.name, source)
        if present(target) or present(sidecar(target)):
            # Check both copies. A second pass must not bless a corrupted source.
            if verified_backup(source) != verified_backup(target):
                raise ValueError("The existing replica differs from the source backup")
            return data

        def copy(stream):
            with source.open("rb") as av:
                shutil.copyfileobj(av, stream, length=1024 * 1024)

        return create_pair(target, copy, data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--keep", type=int, default=2)
    parser.add_argument("--replicate", type=Path, metavar="ARCHIVE")
    parser.add_argument("--replica-directory", type=Path)
    args = parser.parse_args()
    if not 2 <= args.keep <= 30:
        parser.error("Keep between 2 and 30 production recovery copies")
    if args.replicate or args.replica_directory:
        if (
            not args.replicate
            or not args.replica_directory
            or args.output
            or args.production
        ):
            parser.error(
                "Use --replicate ARCHIVE and --replica-directory DIRECTORY together"
            )
        print(json.dumps(replicate(args.replicate, args.replica_directory)))
        return

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / ".local/backups" / f"quantix-{timestamp}.dump"
    if output.name.startswith("."):
        parser.error("The archive name must be visible")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with backup_directory(output.parent):
        recover_stages(output.parent)
        if present(output) or present(sidecar(output)):
            metadata = verified_backup(output)
            if args.production != metadata["production"]:
                raise ValueError(
                    "Existing backup belongs to a different database scope"
                )
        else:
            metadata = create_pair(
                output,
                lambda stream: dump_archive(stream, production=args.production),
                {
                    "kind": "PRIVATE_POSTGRESQL_BACKUP",
                    "generated_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "production": args.production,
                    "restore_verified": False,
                },
            )
        if args.production and args.output is None:
            rotate_backups(output.parent, args.keep)
    print(json.dumps(metadata))


def interrupted(signum, _frame):
    raise SystemExit(f"Backup interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc)) from None
