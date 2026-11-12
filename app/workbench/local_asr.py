"""Local Whisper model management and per-task transcription options."""
from __future__ import annotations

import importlib.util
import importlib
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from .database import PROJECT_ROOT, Database

MODEL_ROOT = PROJECT_ROOT / 'data' / 'models' / 'faster-whisper'
MODELS = ('tiny', 'base', 'small', 'medium', 'large-v3')
_VOCABULARIES = ('vocabulary.txt', 'vocabulary.json')
_FILES = ('config.json', 'model.bin', 'tokenizer.json', 'preprocessor_config.json', *_VOCABULARIES)
_REQUIRED = ('config.json', 'model.bin', 'tokenizer.json')
_lock = threading.Lock()
_previews: dict[str, dict] = {}
_jobs: dict[str, dict] = {}
_installation: dict = {'state': 'idle', 'message': ''}


def dependencies_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ('faster_whisper', 'opencc'))


def start_installation() -> None:
    with _lock:
        if _installation['state'] == 'installing':
            raise ValueError('转写组件正在安装，请稍候。')
        if dependencies_installed():
            raise ValueError('转写组件已安装。')
        _installation.update(state='installing', message='正在安装转写组件，请保持工作台运行。')

    def worker() -> None:
        try:
            result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                 'faster-whisper>=1.1,<2', 'opencc-python-reimplemented>=0.1.7,<0.2'],
                cwd=PROJECT_ROOT, capture_output=True, text=True, errors='replace', timeout=900, check=False,
            )
            importlib.invalidate_caches()
            if result.returncode != 0 or not dependencies_installed():
                raise RuntimeError('安装命令失败或组件未通过检查')
            with _lock:
                _installation.update(state='ready', message='转写组件已安装，请继续下载模型。')
        except Exception:
            logging.getLogger(__name__).exception('本机转写组件安装失败')
            with _lock:
                _installation.update(state='failed', message='安装失败，请检查网络和磁盘空间后重试，或使用手动安装说明。')

    threading.Thread(target=worker, name='local-asr-install', daemon=True).start()


def resolve_device(device: str) -> str:
    if device != 'auto':
        return device
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return 'cpu'
        loader = ctypes.WinDLL if os.name == 'nt' else ctypes.CDLL
        libraries = ('cublas64_12.dll', 'cublasLt64_12.dll', 'cudnn64_9.dll') if os.name == 'nt' else ('libcublas.so.12', 'libcublasLt.so.12', 'libcudnn.so.9')
        for name in libraries:
            loader(name)
        return 'cuda'
    except (ImportError, OSError, RuntimeError, AttributeError):
        return 'cpu'


def _model_dir(model: str) -> Path:
    if model not in MODELS:
        raise ValueError('不支持的本地模型。')
    return MODEL_ROOT / model


def model_ready(model: str) -> bool:
    folder = _model_dir(model)
    try:
        manifest = json.loads((folder / 'workbench-model.json').read_text(encoding='utf-8'))
        sizes = manifest['files']
        def matches(name: str) -> bool:
            return (isinstance(sizes.get(name), int) and sizes[name] > 0 and
                    (folder / name).is_file() and (folder / name).stat().st_size == sizes[name])
        return all(matches(name) for name in _REQUIRED) and any(matches(name) for name in _VOCABULARIES)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def status() -> dict:
    with _lock:
        jobs = {name: dict(item) for name, item in _jobs.items()}
        installation = dict(_installation)
    return {'installed': dependencies_installed(), 'installation': installation,
            'models': [{'id': name, 'ready': model_ready(name), 'job': jobs.get(name)} for name in MODELS],
            'model_root': str(MODEL_ROOT)}


def preview_model(model: str) -> dict:
    folder = _model_dir(model)
    from huggingface_hub import HfApi
    info = HfApi().model_info(f'Systran/faster-whisper-{model}', files_metadata=True, token=False)
    revision = info.sha or ''
    if not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('无法确认模型版本。')
    files = {item.rfilename: item.size for item in info.siblings or [] if item.rfilename in _FILES}
    if (any(not isinstance(files.get(name), int) or files[name] <= 0 for name in _REQUIRED)
            or not any(isinstance(files.get(name), int) and files[name] > 0 for name in _VOCABULARIES)):
        raise ValueError('无法确定模型文件大小，请稍后重试。')
    files = {name: size for name, size in files.items() if isinstance(size, int) and size > 0}
    result = {'model': model, 'revision': revision, 'size_bytes': sum(files.values()),
              'directory': str(folder), 'files': files}
    with _lock:
        _previews[model] = result
    return result


def start_download(model: str, revision: str) -> None:
    folder = _model_dir(model)
    if model_ready(model):
        raise ValueError('模型已准备完成，无需重新下载。')
    with _lock:
        preview = _previews.get(model)
        if not preview or preview['revision'] != revision:
            raise ValueError('请先重新查看模型下载信息。')
        if any(job['state'] == 'downloading' for job in _jobs.values()):
            raise ValueError('已有模型正在下载，请稍后再试。')
        _jobs[model] = {'state': 'downloading', 'message': '模型下载中'}

    def worker() -> None:
        try:
            from huggingface_hub import snapshot_download
            folder.mkdir(parents=True, exist_ok=True)
            snapshot_download(repo_id=f'Systran/faster-whisper-{model}', revision=revision,
                              local_dir=str(folder), allow_patterns=list(preview['files']), token=False)
            if any(not (folder / name).is_file() or (folder / name).stat().st_size != size
                   for name, size in preview['files'].items()):
                raise ValueError('下载文件不完整')
            manifest = folder / 'workbench-model.json'
            temporary = folder / 'workbench-model.json.tmp'
            temporary.write_text(json.dumps({'revision': revision, 'files': preview['files']}), encoding='utf-8')
            temporary.replace(manifest)
            with _lock:
                _jobs[model] = {'state': 'ready', 'message': '模型已准备完成'}
        except Exception:
            with _lock:
                _jobs[model] = {'state': 'failed', 'message': '下载失败，请检查网络、磁盘空间后重新预览并重试。'}

    threading.Thread(target=worker, name=f'whisper-download-{model}', daemon=True).start()


def save_task_options(database: Database, task_id: str, *, model: str, language: str, device: str) -> None:
    _model_dir(model)
    if language not in {'auto', 'zh', 'en'} or device not in {'auto', 'cpu', 'cuda'}:
        raise ValueError('本地转写选项无效。')
    with database.transaction(write=True) as connection:
        connection.execute('INSERT INTO local_transcription_options (task_id,model,language,device) VALUES (?,?,?,?)',
                           (task_id, model, language, device))


def get_task_options(database: Database, task_id: str) -> dict | None:
    with database.transaction() as connection:
        step_config = connection.execute(
            "SELECT mode, local_model, local_language, local_device "
            "FROM transcription_step_configs WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if step_config is not None:
            if step_config["mode"] == "cloud":
                return None
            return {
                "model": step_config["local_model"],
                "language": step_config["local_language"],
                "device": step_config["local_device"],
            }
        row = connection.execute('SELECT model,language,device FROM local_transcription_options WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None
