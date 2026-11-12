"""Production static serving for the Vue workbench."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def frontend_index_path() -> Path | None:
    index = FRONTEND_DIST / "index.html"
    return index if index.is_file() else None


class WorkbenchStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            # Vue Router uses history mode. Only extensionless unknown paths
            # fall back to the SPA entry; missing assets remain real 404s.
            if (
                exc.status_code == 404
                and not Path(path).suffix
                and not path.casefold().startswith("assets/")
                and frontend_index_path() is not None
            ):
                return await super().get_response("index.html", scope)
            raise


def mount_frontend(app: FastAPI) -> bool:
    if frontend_index_path() is None:
        return False
    app.mount("/", WorkbenchStaticFiles(directory=FRONTEND_DIST, html=True), name="workbench-frontend")
    return True
