from __future__ import annotations

from pathlib import Path

from app.build_info import calculate_build_id


def test_frontend_dev_build_id_ignores_production_dist(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    generated_page = dist / "index.html"
    generated_page.write_text("production build one", encoding="utf-8")
    monkeypatch.setenv("OMD_FRONTEND_DEV", "1")

    first_build_id = calculate_build_id(tmp_path)
    generated_page.write_text("production build two", encoding="utf-8")

    assert calculate_build_id(tmp_path) == first_build_id


def test_production_build_id_includes_frontend_dist(tmp_path, monkeypatch) -> None:
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    generated_page = dist / "index.html"
    generated_page.write_text("production build one", encoding="utf-8")
    monkeypatch.delenv("OMD_FRONTEND_DEV", raising=False)

    first_build_id = calculate_build_id(tmp_path)
    generated_page.write_text("production build two", encoding="utf-8")

    assert calculate_build_id(tmp_path) != first_build_id


def test_start_local_uses_workbench_api_with_frontend_dev_flag() -> None:
    startup_script = Path(__file__).resolve().parents[1] / "start-local.cmd"
    script_text = startup_script.read_text(encoding="utf-8")

    assert "set OMD_FRONTEND_DEV=1" in script_text
    assert "--asgi-app anonymous_poc:app --frontend-dev" in script_text
