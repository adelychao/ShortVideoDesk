"""Read-only local runtime checks. This module never makes network requests."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from typing import Any

from . import local_asr
from .config import AppConfig, DEFAULT_SETTINGS
from .credentials import CredentialStoreUnavailable, get_model_api_key
from .model_profiles import get_default_model_profile, profile_api_key
from .providers import get_provider_preset

_ALL_STEPS = [
    "download", "import", "transcribe", "ocr", "comments", "analyze_comments",
    "analyze_structure", "knowledge_cards", "generate_scripts", "index", "write_obsidian",
]


def run_diagnostics(config: AppConfig | None = None, frontend_dist: Path | None = None) -> list[dict[str, Any]]:
    selected_config = config or AppConfig()
    try:
        settings = get_default_model_profile(selected_config) or selected_config.get_settings()
    except Exception:
        settings = dict(DEFAULT_SETTINGS)
    results: list[dict[str, Any]] = []

    python_ok = sys.version_info >= (3, 11)
    results.append(_result(
        "python",
        "available" if python_ok else "error",
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}" if python_ok else "需要 Python 3.11 或更新版本。",
        None if python_ok else "安装 Python 3.11 x64 或更新版本，并在项目环境中重新安装依赖。",
        ["download", "transcribe", "ocr", "comments"],
    ))

    for package_name, import_name, affected in (
        ("fastapi", "FastAPI", ["download"]),
        ("pydantic", "Pydantic", ["download"]),
        ("uvicorn", "Uvicorn", ["download"]),
        ("yt-dlp", "yt-dlp", ["download"]),
        ("playwright", "Playwright", ["download"]),
        ("keyring", "系统凭据存储", ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]),
        ("litellm", "LiteLLM 模型适配器", ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]),
    ):
        try:
            package_version = version(package_name)
        except PackageNotFoundError:
            results.append(_result(
                f"dependency-{package_name}", "error", f"缺少 {import_name} 依赖。",
                "在项目 Python 环境中安装 requirements-poc.txt。", affected,
            ))
        except Exception:
            results.append(_result(f"dependency-{package_name}", "limited", f"无法读取 {import_name} 版本。", None, affected))
        else:
            results.append(_result(
                f"dependency-{package_name}", "available", f"{import_name} {package_version} 已安装。", None, affected
            ))

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser_executable = Path(playwright.chromium.executable_path)
        browser_ready = browser_executable.is_file()
    except Exception:
        browser_ready = False
    results.append(_result(
        "playwright-browser",
        "available" if browser_ready else "unconfigured",
        "Playwright Chromium 浏览器已安装。" if browser_ready else "未检测到 Playwright Chromium 浏览器。",
        None if browser_ready else "在项目 Python 环境中运行 playwright install chromium。",
        ["download", "comments"],
    ))

    try:
        local_status = local_asr.status()
        local_asr_installed = local_status.get("installed") is True
        results.append(_result(
            "dependency-faster-whisper",
            "available" if local_asr_installed else "unconfigured",
            "本机转写组件已安装。"
            if local_asr_installed
            else "本机转写组件尚未安装。",
            None
            if local_asr_installed
            else "在“知识库与设置 → 模型与转写”中启用本机转写。",
            ["transcribe"],
        ))
        models = local_status.get("models")
        ready_models = [
            item.get("id")
            for item in models
            if isinstance(item, dict) and item.get("ready") is True and isinstance(item.get("id"), str)
        ] if isinstance(models, list) else []
        results.append(_result(
            "local-asr-model",
            "available" if ready_models else "unconfigured",
            f"本地语音模型已准备：{'、'.join(ready_models)}。"
            if ready_models
            else "尚未准备本地语音模型；云端转写不受影响。",
            None
            if ready_models
            else (
                "先在“知识库与设置 → 模型与转写”中安装本机转写组件，再下载模型。"
                if not local_asr_installed
                else "在“知识库与设置 → 模型与转写”中预览并下载一个本地模型。"
            ),
            ["transcribe"],
        ))
    except Exception:
        results.append(_result(
            "dependency-faster-whisper", "error", "无法读取本机转写环境状态。",
            "检查工作台运行环境和模型目录权限。", ["transcribe"],
        ))

    results.append(_check_executable("ffmpeg", ["transcribe", "ocr", "download"]))
    results.append(_check_executable("ffprobe", ["download", "transcribe", "ocr"]))

    try:
        selected_config.database.migrate()
        with selected_config.database.transaction() as connection:
            connection.execute("SELECT 1").fetchone()
        results.append(_result("sqlite", "available", f"SQLite {sqlite3.sqlite_version} 可读写。", None, _ALL_STEPS))
    except Exception:
        results.append(_result("sqlite", "error", "本机任务数据库不可用。", "检查项目 data 目录的读写权限和磁盘空间。", _ALL_STEPS))

    directory_settings = (
        ("archive-root", "archive_root", "归档目录", True, ["download", "import"]),
        ("obsidian-reference-root", "obsidian_reference_root", "Obsidian 参考目录", False, ["generate_scripts"]),
        ("obsidian-output-root", "obsidian_output_root", "Obsidian 输出目录", True, ["write_obsidian"]),
    )
    for check_id, key, label, needs_write, affected in directory_settings:
        raw = settings.get(key)
        if not isinstance(raw, str) or not raw:
            results.append(_result(check_id, "unconfigured", f"{label}尚未配置。", f"在“知识库与设置”中选择本机目录。", affected))
            continue
        path = Path(raw)
        readable = path.is_dir() and os.access(path, os.R_OK)
        writable = path.is_dir() and (not needs_write or os.access(path, os.W_OK))
        if readable and writable:
            results.append(_result(check_id, "available", f"{label}可访问。", None, affected))
        else:
            results.append(_result(
                check_id, "error", f"{label}不可读写或目录已失效。",
                "重新选择存在且权限正确的本机文件夹。", affected,
            ))

    provider = settings.get("model_provider")
    model_id = settings.get("model_id")
    if not provider or not model_id:
        results.append(_result("model", "unconfigured", "模型服务商和模型尚未配置。", "服务商与具体模型将在本机设置页配置。", ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]))
    else:
        try:
            configured_key = bool(
                profile_api_key(settings)
                if isinstance(settings.get("credential_id"), str)
                else get_model_api_key(str(provider))
            )
        except CredentialStoreUnavailable:
            results.append(_result("model", "error", "系统凭据存储不可用。", "安装 keyring 依赖并确认 Windows 凭据管理器可用。", ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]))
        else:
            preset = get_provider_preset(provider)
            provider_label = preset.label if preset else "所选服务商"
            if configured_key:
                results.append(_result("model", "available", f"{provider_label}与文本/图像模型 ID 已配置，本机凭据已保存；每个云端步骤仍需在任务中单独授权。", None, ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]))
            else:
                results.append(_result("model", "unconfigured", f"{provider_label} API Key 尚未存入本机凭据管理器。", "在本机设置页为当前服务商保存 API Key。", ["transcribe", "ocr", "analyze_comments", "analyze_structure", "knowledge_cards", "generate_scripts"]))

    dist = frontend_dist or Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "index.html").is_file():
        results.append(_result("frontend", "available", "本地工作台生产前端已构建。", None, _ALL_STEPS))
    else:
        results.append(_result("frontend", "limited", "尚未找到生产前端构建文件。", "在 frontend 目录运行 npm run build；开发时可启动 Vite。", _ALL_STEPS))
    return results


def _check_executable(name: str, affected: list[str]) -> dict[str, Any]:
    executable = shutil.which(name)
    if not executable:
        return _result(name, "unconfigured", f"未检测到 {name}。", "安装 FFmpeg 并将 ffmpeg.exe 与 ffprobe.exe 加入 PATH。", affected)
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _result(name, "error", f"{name} 无法启动。", "检查程序安装和 PATH 配置。", affected)
    if result.returncode != 0:
        return _result(name, "error", f"{name} 启动失败。", "检查程序安装和 PATH 配置。", affected)
    return _result(name, "available", f"{name} 已安装并可启动。", None, affected)


def _result(
    result_id: str,
    status: str,
    message: str,
    remediation: str | None,
    affected_steps: list[str],
) -> dict[str, Any]:
    return {
        "id": result_id,
        "status": status,
        "message": message[:300],
        "remediation": remediation[:300] if remediation else None,
        "affected_steps": affected_steps,
    }
