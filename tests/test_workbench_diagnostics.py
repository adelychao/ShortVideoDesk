from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.workbench import diagnostics
from app.workbench.config import AppConfig
from app.workbench.database import Database


@pytest.mark.parametrize(
    ("installed", "ready_models", "dependency_status", "model_status"),
    [
        (False, [], "unconfigured", "unconfigured"),
        (True, [], "available", "unconfigured"),
        (True, ["small"], "available", "available"),
    ],
)
def test_diagnostics_report_local_asr_dependency_and_prepared_models(
    tmp_path,
    monkeypatch,
    installed,
    ready_models,
    dependency_status,
    model_status,
):
    models = [
        {"id": model_id, "ready": model_id in ready_models, "job": None}
        for model_id in ("tiny", "base", "small", "medium", "large-v3")
    ]
    monkeypatch.setattr(
        diagnostics,
        "local_asr",
        SimpleNamespace(
            status=lambda: {"installed": installed, "models": models, "model_root": "private-path"}
        ),
        raising=False,
    )
    config = AppConfig(Database(tmp_path / "workbench.sqlite3"))

    results = diagnostics.run_diagnostics(config, frontend_dist=tmp_path / "dist")
    by_id = {item["id"]: item for item in results}

    assert by_id["dependency-faster-whisper"]["status"] == dependency_status
    assert by_id["local-asr-model"]["status"] == model_status
    assert "private-path" not in str(by_id["local-asr-model"])
    if ready_models:
        assert "small" in by_id["local-asr-model"]["message"]
    elif installed:
        assert "下载" in by_id["local-asr-model"]["remediation"]
    else:
        assert "安装" in by_id["local-asr-model"]["remediation"]
