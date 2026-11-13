"""Hold the existing project runtime lock while the Windows bootstrap edits the venv."""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(project_root))

    try:
        from app.runtime import ProjectLock, ProjectLockHeldError
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 1

    lock = ProjectLock(project_root / "data" / "runtime" / "project.lock")
    try:
        lock.acquire(blocking=False)
    except ProjectLockHeldError:
        print("BUSY", flush=True)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        return 1

    print("LOCKED", flush=True)
    try:
        sys.stdin.readline()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
