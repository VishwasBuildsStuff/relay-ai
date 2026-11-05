"""Dry-run-first purge for old, terminal RELAY AI tasks."""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import uuid
from pathlib import Path

from storage import SQLiteTaskStore


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def purge_tasks(older_than_days: int, apply: bool = False) -> int:
    cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=older_than_days)
    cutoff = cutoff_time.isoformat()
    database_path = os.environ.get(
        "RELAY_DB_PATH", str(Path(__file__).with_name("relay_tasks.sqlite3"))
    )
    applied_root = Path(
        os.environ.get("RELAY_APPLIED_ROOT", str(Path(__file__).with_name(".relay_applied")))
    ).resolve()
    store = SQLiteTaskStore(database_path)
    candidates = store.retention_candidates(cutoff)
    mode = "APPLY" if apply else "DRY RUN"
    print(f"{mode}: {len(candidates)} terminal tasks older than {cutoff_time.isoformat()} (UTC).")
    for row in candidates:
        print(f"{row['task_id']}\t{row['status']}\t{row['updated_at']}")
    if not apply:
        print("No data deleted. Use --apply --confirm-delete to purge these candidates.")
        return 0

    failures = 0
    for row in candidates:
        task_id = row["task_id"]
        target = applied_root / task_id
        trash = applied_root / f".retention-delete-{uuid.uuid4().hex}"
        moved = False
        database_deleted = False
        try:
            if target.is_symlink():
                raise RuntimeError("Refusing to purge a task workspace symlink.")
            resolved = target.resolve(strict=False)
            if not resolved.is_relative_to(applied_root):
                raise RuntimeError("Task workspace path escapes RELAY_APPLIED_ROOT.")
            if target.exists():
                os.replace(target, trash)
                moved = True
            if not store.delete_retained_task(task_id, cutoff):
                if moved:
                    os.replace(trash, target)
                print(f"SKIPPED {task_id}: task no longer meets the retention rule.")
                continue
            database_deleted = True
            if moved:
                _remove_path(trash)
            print(f"PURGED {task_id}")
        except Exception as error:
            failures += 1
            if database_deleted:
                print(f"ERROR {task_id}: task data was removed from SQLite but workspace cleanup failed at {trash}: {error}", file=sys.stderr)
                continue
            if moved and trash.exists() and not database_deleted:
                try:
                    os.replace(trash, target)
                except OSError as restore_error:
                    print(f"ERROR {task_id}: {error}; workspace restore also failed: {restore_error}", file=sys.stderr)
                    continue
            print(f"ERROR {task_id}: {error}", file=sys.stderr)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--older-than-days", type=int, required=True, help="Minimum age since last task update.")
    parser.add_argument("--apply", action="store_true", help="Delete eligible task records and task-owned workspaces.")
    parser.add_argument("--confirm-delete", action="store_true", help="Required together with --apply.")
    args = parser.parse_args()
    if args.older_than_days < 1:
        parser.error("--older-than-days must be at least 1")
    if args.apply != args.confirm_delete:
        parser.error("use --apply --confirm-delete together; omit both for a dry run")
    return purge_tasks(args.older_than_days, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
