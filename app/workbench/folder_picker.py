"""本机交互式文件夹选择。"""

from __future__ import annotations

import os
from threading import Lock

_picker_lock = Lock()


def _ask_open_filename(*, title: str, filetypes: list[tuple[str, str]]) -> str | None:
    if os.name != "nt":
        raise RuntimeError("file selection is only available on Windows")
    if not _picker_lock.acquire(blocking=False):
        raise RuntimeError("a file picker is already open")
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askopenfilename(parent=root, title=title, filetypes=filetypes)
        return str(selected) if selected else None
    finally:
        try:
            if root is not None:
                root.destroy()
        finally:
            _picker_lock.release()


def choose_local_video() -> str | None:
    return _ask_open_filename(
        title="选择原创视频",
        filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.webm")],
    )


def choose_local_media() -> str | None:
    return _ask_open_filename(
        title="选择本地媒体",
        filetypes=[("媒体文件", "*.mp4 *.mov *.mkv *.webm *.mp3 *.m4a *.wav")],
    )


def choose_local_directory() -> str | None:
    if os.name != "nt":
        raise RuntimeError("folder selection is only available on Windows")
    if not _picker_lock.acquire(blocking=False):
        raise RuntimeError("folder selection is already open")
    root = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(parent=root, title="选择文件夹", mustexist=True)
        return str(selected) if selected else None
    finally:
        try:
            if root is not None:
                root.destroy()
        finally:
            _picker_lock.release()
