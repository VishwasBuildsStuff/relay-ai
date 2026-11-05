"""Create a permission-restricted, SQLite-consistent RELAY AI backup archive."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from pathlib import PurePosixPath

MAX_BACKUP_MEMBERS = 100_000
MAX_BACKUP_UNPACKED_BYTES = 100 * 1024 * 1024 * 1024
MAX_BACKUP_MANIFEST_BYTES = 64 * 1024


def create_backup(output_dir: Path) -> Path:
    source_db = Path(os.environ.get("RELAY_DB_PATH", str(Path(__file__).with_name("relay_tasks.sqlite3")))).resolve()
    applied_root = Path(os.environ.get("RELAY_APPLIED_ROOT", str(Path(__file__).with_name(".relay_applied")))).resolve()
    if not source_db.is_file():
        raise FileNotFoundError(f"RELAY database does not exist: {source_db}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir = output_dir.resolve()
    if output_dir == applied_root or output_dir.is_relative_to(applied_root):
        raise ValueError("Backup output must not be inside RELAY_APPLIED_ROOT.")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = output_dir / f"relay-ai-backup-{stamp}.tar.gz"
    if archive_path.exists():
        raise FileExistsError(archive_path)

    with tempfile.TemporaryDirectory(prefix="relay-backup-") as temporary_dir:
        snapshot_db = Path(temporary_dir) / "relay_tasks.sqlite3"
        with sqlite3.connect(source_db) as source, sqlite3.connect(snapshot_db) as destination:
            source.backup(destination)

        file_descriptor, temporary_archive_name = tempfile.mkstemp(
            prefix=f".{archive_path.name}.", suffix=".tmp", dir=output_dir
        )
        temporary_archive = Path(temporary_archive_name)
        try:
            os.fchmod(file_descriptor, 0o600)
            with os.fdopen(file_descriptor, "wb") as archive_file:
                with tarfile.open(fileobj=archive_file, mode="w:gz") as archive:
                    archive.add(snapshot_db, arcname="relay_tasks.sqlite3")
                    manifest = {
                        "format_version": 1,
                        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "database": source_db.name,
                        "includes_task_workspaces": applied_root.is_dir(),
                    }
                    manifest_path = Path(temporary_dir) / "backup_manifest.json"
                    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
                    archive.add(manifest_path, arcname="backup_manifest.json")
                    if applied_root.is_dir():
                        for entry in sorted(applied_root.iterdir()):
                            if entry.name.startswith(".deleting-") or entry.name == ".target-locks":
                                continue
                            archive.add(entry, arcname=f"relay_applied/{entry.name}", recursive=True)
            verify_backup(temporary_archive)
            os.replace(temporary_archive, archive_path)
        except Exception:
            temporary_archive.unlink(missing_ok=True)
            raise
    return archive_path


def verify_backup(archive_path: Path) -> dict[str, object]:
    """Inspect archive structure and run SQLite integrity checks without extracting workspaces."""
    archive_path = archive_path.resolve(strict=True)
    seen: set[str] = set()
    total_bytes = 0
    database_member = None
    manifest_member = None
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_BACKUP_MEMBERS:
            raise ValueError("Backup contains too many archive entries.")
        for member in members:
            member_path = member.name.rstrip("/") if member.isdir() else member.name
            if (
                not member_path or member_path.startswith("/")
                or any(part in {"", ".", ".."} for part in member_path.split("/"))
            ):
                raise ValueError("Backup contains an unsafe member path.")
            path = PurePosixPath(member_path)
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ValueError("Backup contains an unsupported link or special file.")
            if member.isdir():
                continue
            if member.name in seen:
                raise ValueError("Backup contains duplicate file paths.")
            seen.add(member.name)
            total_bytes += member.size
            if total_bytes > MAX_BACKUP_UNPACKED_BYTES:
                raise ValueError("Backup expands beyond the 100 GiB verification limit.")
            if member.name == "relay_tasks.sqlite3":
                database_member = member
            elif member.name == "backup_manifest.json":
                manifest_member = member
            elif len(path.parts) < 2 or path.parts[0] != "relay_applied":
                raise ValueError("Backup contains an unexpected file outside its known archive roots.")
        if database_member is None or manifest_member is None:
            raise ValueError("Backup is missing its SQLite database or manifest.")
        if manifest_member.size > MAX_BACKUP_MANIFEST_BYTES:
            raise ValueError("Backup manifest exceeds its size limit.")
        manifest_stream = archive.extractfile(manifest_member)
        database_stream = archive.extractfile(database_member)
        if manifest_stream is None or database_stream is None:
            raise ValueError("Backup metadata could not be read.")
        manifest = json.load(manifest_stream)
        if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
            raise ValueError("Backup manifest format is unsupported.")
        with tempfile.TemporaryDirectory(prefix="relay-backup-verify-") as temporary_dir:
            database_path = Path(temporary_dir) / "relay_tasks.sqlite3"
            with database_path.open("wb") as database_file:
                shutil.copyfileobj(database_stream, database_file, length=1024 * 1024)
            with sqlite3.connect(database_path) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
                schema_version = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()[0]
            if integrity != "ok" or foreign_key_errors:
                raise ValueError("Backup SQLite integrity or foreign-key check failed.")
    return {
        "archive": str(archive_path),
        "format_version": manifest["format_version"],
        "database_schema_version": schema_version,
        "includes_task_workspaces": manifest.get("includes_task_workspaces") is True,
        "entries": len(seen),
        "unpacked_bytes": total_bytes,
        "sqlite_integrity": "ok",
        "foreign_key_errors": 0,
    }


def restore_backup(archive_path: Path, destination: Path) -> Path:
    """Safely restore into a new isolated directory without replacing live data."""
    verification = verify_backup(archive_path)
    archive_path = archive_path.resolve(strict=True)
    destination = destination.expanduser()
    if destination.name in {"", ".", ".."} or destination.is_symlink() or destination.exists():
        raise FileExistsError("Restore destination must be a new, non-symlink directory path.")
    destination = destination.parent.resolve(strict=False) / destination.name
    applied_root = Path(os.environ.get("RELAY_APPLIED_ROOT", str(Path(__file__).with_name(".relay_applied")))).resolve()
    if destination == applied_root or destination.is_relative_to(applied_root):
        raise ValueError("Restore destination must not be inside RELAY_APPLIED_ROOT.")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Restore destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    os.chmod(staging, 0o700)
    try:
        if verification["includes_task_workspaces"]:
            (staging / "relay_applied").mkdir(mode=0o700)
        with tarfile.open(archive_path, mode="r:gz") as archive:
            for member in archive.getmembers():
                normalized_name = member.name.rstrip("/") if member.isdir() else member.name
                if normalized_name in {"relay_tasks.sqlite3", "backup_manifest.json"}:
                    relative_parts = (normalized_name,)
                elif normalized_name.startswith("relay_applied/"):
                    relative_parts = ("relay_applied", *PurePosixPath(normalized_name).parts[1:])
                else:
                    continue
                if any(
                    part in {"", ".", ".."} or ":" in part or "\\" in part
                    or part.endswith((" ", ".")) or any(ord(char) < 32 or ord(char) == 127 for char in part)
                    for part in relative_parts
                ):
                    raise ValueError("Backup contains a non-portable restore path.")
                target = staging.joinpath(*relative_parts)
                if not target.resolve(strict=False).is_relative_to(staging):
                    raise ValueError("Backup restore path escapes its staging directory.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("Backup member could not be read during restore.")
                written = 0
                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > member.size:
                            raise ValueError("Backup member exceeds its declared size.")
                        output.write(chunk)
                if written != member.size:
                    raise ValueError("Backup member size does not match its archive header.")
        with sqlite3.connect(staging / "relay_tasks.sqlite3") as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Restored SQLite database failed its integrity check.")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Restored SQLite database failed its foreign-key check.")
        os.replace(staging, destination)
        return destination
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def check_latest_backup(output_dir: Path, max_age_hours: float) -> dict[str, object]:
    """Return a monitoring-friendly result for the newest generated archive."""
    if max_age_hours <= 0:
        raise ValueError("Maximum backup age must be greater than zero hours.")
    output_dir = output_dir.resolve(strict=True)
    archives = [
        path for path in output_dir.glob("relay-ai-backup-*.tar.gz")
        if path.is_file() and not path.is_symlink()
    ]
    if not archives:
        return {"ok": False, "error": "No backup archives found.", "output_dir": str(output_dir)}
    newest = max(archives, key=lambda path: path.stat().st_mtime)
    created = datetime.datetime.fromtimestamp(newest.stat().st_mtime, datetime.timezone.utc)
    age_hours = (datetime.datetime.now(datetime.timezone.utc) - created).total_seconds() / 3600
    return {
        "ok": age_hours <= max_age_hours,
        "latest_archive": str(newest),
        "modified_at": created.isoformat(),
        "age_hours": round(age_hours, 2),
        "max_age_hours": max_age_hours,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, nargs="?", help="Directory outside RELAY_APPLIED_ROOT for a new backup archive")
    parser.add_argument("--verify", type=Path, help="Verify an existing backup archive without extracting it")
    parser.add_argument("--restore-archive", type=Path, help="Restore an archive into a new isolated directory")
    parser.add_argument("--restore-to", type=Path, help="New destination directory used with --restore-archive")
    parser.add_argument("--check-latest", type=Path, help="Check whether a recent backup exists in this directory")
    parser.add_argument("--max-age-hours", type=float, default=26, help="Maximum age for --check-latest (default: 26)")
    args = parser.parse_args()
    if args.check_latest:
        if args.verify or args.restore_archive or args.restore_to or args.output_dir:
            parser.error("--check-latest cannot be combined with backup creation, verify, or restore options")
        result = check_latest_backup(args.check_latest, args.max_age_hours)
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    if args.verify and (args.restore_archive or args.restore_to or args.output_dir):
        parser.error("--verify cannot be combined with backup creation or restore options")
    if args.restore_archive or args.restore_to:
        if not args.restore_archive or not args.restore_to or args.output_dir or args.verify:
            parser.error("use --restore-archive ARCHIVE --restore-to NEW_EMPTY_PATH")
        print(restore_backup(args.restore_archive, args.restore_to))
    elif args.verify:
        print(json.dumps(verify_backup(args.verify), indent=2))
    elif args.output_dir:
        print(create_backup(args.output_dir))
    else:
        parser.error("provide an output directory or --verify ARCHIVE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
