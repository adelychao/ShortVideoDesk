"""Local OCR availability and per-task recognition options."""
from __future__ import annotations

import importlib.util
import hashlib
import math
from pathlib import Path

from .database import Database

_MODELS = {
    "Det": ("PP-OCRv6_det_small.onnx", "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f"),
    "Rec": ("PP-OCRv6_rec_small.onnx", "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"),
    "Cls": ("ch_ppocr_mobile_v2.0_cls_mobile.onnx", "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"),
}

OCR_SAMPLING_VERSION = 2
OCR_WORKERS = 2
SUBTITLE_CROP_RATIO = 0.42


def validate_interval(interval: float) -> float:
    if isinstance(interval, bool) or not isinstance(interval, (float, int)) or not math.isfinite(interval) or not 0.5 <= interval <= 60:
        raise ValueError("OCR 抽帧间隔须在 0.5 到 60 秒之间。")
    return float(interval)


def status() -> dict:
    spec = importlib.util.find_spec("rapidocr")
    installed = spec is not None and importlib.util.find_spec("onnxruntime") is not None
    ready = False
    if installed:
        try:
            model_paths()
            ready = True
        except (OSError, ValueError):
            ready = False
    runtime = runtime_info()
    return {"installed": installed, "ready": ready, "model": "PP-OCRv6 small", "device": runtime["device"],
            "providers": runtime["providers"], "workers": OCR_WORKERS}


def runtime_info() -> dict[str, object]:
    """Report the actual ONNX Runtime provider, preferring CUDA when available."""
    try:
        import onnxruntime as ort

        preload = getattr(ort, "preload_dlls", None)
        if callable(preload):
            try:
                preload(directory="")
            except (OSError, RuntimeError, TypeError):
                pass
        providers = list(ort.get_available_providers())
        cuda = "CUDAExecutionProvider" in providers
        return {"device": "cuda" if cuda and ort.get_device().upper() == "GPU" else "cpu",
                "providers": providers}
    except (ImportError, AttributeError, RuntimeError):
        return {"device": "cpu", "providers": []}


def engine_params() -> dict[str, object]:
    params: dict[str, object] = model_paths()
    runtime = runtime_info()
    params["EngineConfig.onnxruntime.use_cuda"] = runtime["device"] == "cuda"
    params["EngineConfig.onnxruntime.intra_op_num_threads"] = 1 if runtime["device"] == "cuda" else 2
    params["EngineConfig.onnxruntime.inter_op_num_threads"] = 1
    return params


def model_paths() -> dict[str, str]:
    spec = importlib.util.find_spec("rapidocr")
    if spec is None or spec.origin is None:
        raise ValueError("本地 OCR 未安装。")
    folder = Path(spec.origin).parent / "models"
    paths = {}
    for section, (name, expected) in _MODELS.items():
        path = folder / name
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise ValueError("本地 OCR 模型文件校验失败。")
        paths[f"{section}.model_path"] = str(path)
    return paths


def validate_scan_profile(profile: str) -> str:
    if profile not in {"full", "subtitles"}:
        raise ValueError("OCR 识别范围无效。")
    return profile


def save_task_options(database: Database, task_id: str, *, mode: str, interval: float,
                      scan_profile: str = "full") -> None:
    if mode not in {"local", "cloud"}:
        raise ValueError("OCR 识别方式无效。")
    interval = validate_interval(interval)
    scan_profile = validate_scan_profile(scan_profile)
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO ocr_task_options (task_id,mode,interval_seconds,scan_profile) VALUES (?,?,?,?)",
                            (task_id, mode, interval, scan_profile))


def get_task_options(database: Database, task_id: str) -> dict:
    with database.transaction() as connection:
        row = connection.execute("SELECT mode, interval_seconds FROM ocr_task_options WHERE task_id=?", (task_id,)).fetchone()
    return {"mode": row["mode"], "interval": float(row["interval_seconds"])} if row else {"mode": "cloud", "interval": 2.0}


def get_scan_profile(database: Database, task_id: str) -> str:
    with database.transaction() as connection:
        row = connection.execute("SELECT scan_profile FROM ocr_task_options WHERE task_id=?", (task_id,)).fetchone()
    return validate_scan_profile(row["scan_profile"]) if row else "full"
