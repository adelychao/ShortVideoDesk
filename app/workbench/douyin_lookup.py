"""通过本机进程管道复用 SAU 的抖音查询浏览器。"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
from pathlib import Path


_PREFIX = "DOUYIN_LOOKUP:"
_MAX_LINE = 1024 * 1024
_LOGGER = logging.getLogger(__name__)


class DouyinLookupError(RuntimeError):
    pass


class DouyinLookup:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._project: Path | None = None
        self._output: queue.Queue[str] = queue.Queue()

    @staticmethod
    def available(project: Path) -> bool:
        return DouyinLookup._python(project).is_file() and (
            project / "uploader/douyin_uploader/lookup_worker.py"
        ).is_file()

    @staticmethod
    def _python(project: Path) -> Path:
        return project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    @staticmethod
    def _read_output(process: subprocess.Popen[str], output: queue.Queue[str]) -> None:
        assert process.stdout is not None
        try:
            while line := process.stdout.readline(_MAX_LINE + 1):
                if len(line) > _MAX_LINE:
                    break
                if line.startswith(_PREFIX):
                    output.put(line[len(_PREFIX):])
        except (OSError, ValueError):
            _LOGGER.debug("读取抖音查询进程输出时管道已关闭。")
        finally:
            process.stdout.close()
            output.put("")

    def close(self) -> None:
        if not self._lock.acquire(timeout=1):
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    _LOGGER.debug("抖音查询进程已退出。")
            return
        try:
            self._close_process()
        finally:
            self._lock.release()

    def _close_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            _LOGGER.debug("抖音查询进程输入管道已关闭。")
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def search(self, project: Path, operation: str, account: str, keyword: str,
               city_code: str | None = None) -> dict:
        if not self._lock.acquire(timeout=45):
            raise DouyinLookupError("抖音查询等待超时，请重试。")
        try:
            if self._process is None or self._process.poll() is not None or self._project != project:
                self._close_process()
                self._process = subprocess.Popen(
                    [str(self._python(project)), "-m", "uploader.douyin_uploader.lookup_worker"],
                    cwd=project,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self._project = project
                self._output = queue.Queue()
                threading.Thread(
                    target=self._read_output, args=(self._process, self._output), daemon=True,
                    name="douyin-lookup-output",
                ).start()
            assert self._process.stdin is not None
            request = {"operation": operation, "account": account, "keyword": keyword, "city_code": city_code}
            self._process.stdin.write(json.dumps(request, ensure_ascii=True) + "\n")
            self._process.stdin.flush()
            reply = json.loads(self._output.get(timeout=45))
            if not isinstance(reply, dict):
                raise ValueError("invalid lookup reply")
        except (OSError, ValueError, queue.Empty, RecursionError):
            self._close_process()
            raise DouyinLookupError("抖音查询会话暂不可用，请重试。") from None
        finally:
            self._lock.release()
        result = reply.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise DouyinLookupError("抖音创作者中心未返回可用的搜索结果，请重试。")
        return result
