"""Local FFmpeg/ffprobe helpers for bounded ASR chunks and OCR frames."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator


ASR_CHUNK_SECONDS = 28.0
MAX_OCR_FRAMES = 8


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    has_audio: bool
    has_video: bool
    audio_duration_seconds: float | None = None
    video_duration_seconds: float | None = None


@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_seconds: float
    end_seconds: float
    index: int


@dataclass(frozen=True)
class OcrFrameFile:
    path: Path
    frame_id: str
    sampled_at_seconds: float
    index: int


def probe_media(path: Path) -> MediaProbe:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ValueError("未检测到 ffprobe。请安装 FFmpeg 并将 ffprobe.exe 加入 PATH。")
    try:
        result = subprocess.run(
            [
                executable,
                "-v", "error",
                "-protocol_whitelist", "file,pipe",
                "-show_entries", "format=duration:stream=codec_type,duration,duration_ts,time_base",
                "-of", "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=60,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("ffprobe 无法读取本次任务媒体。") from None
    if result.returncode != 0:
        raise ValueError("ffprobe 无法识别本次任务媒体。")
    try:
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        format_duration = _positive_finite_float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
        raise ValueError("ffprobe 返回了无效的媒体信息。") from None
    if not isinstance(streams, list):
        raise ValueError("媒体时长无效或没有可处理的流。")
    codec_types = {
        row.get("codec_type")
        for row in streams
        if isinstance(row, dict) and isinstance(row.get("codec_type"), str)
    }
    if not codec_types.intersection({"audio", "video"}):
        raise ValueError("媒体中没有可处理的音视频流。")
    stream_durations: dict[str, list[float]] = {"audio": [], "video": []}
    for row in streams:
        if not isinstance(row, dict):
            continue
        codec_type = row.get("codec_type")
        if not isinstance(codec_type, str) or codec_type not in stream_durations:
            continue
        stream_duration = _positive_finite_float(row.get("duration"))
        if stream_duration is None:
            try:
                duration_ts = int(row["duration_ts"])
                time_base = Fraction(str(row["time_base"]))
                stream_duration = float(duration_ts * time_base)
            except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
                stream_duration = None
        if stream_duration is not None and math.isfinite(stream_duration) and stream_duration > 0:
            stream_durations[codec_type].append(stream_duration)

    audio_duration = max(stream_durations["audio"], default=None)
    video_duration = max(stream_durations["video"], default=None)
    duration = format_duration or max((value for value in (audio_duration, video_duration) if value is not None), default=None)
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise ValueError("媒体时长无效或没有可处理的流。")
    return MediaProbe(
        duration,
        "audio" in codec_types,
        "video" in codec_types,
        audio_duration or (duration if "audio" in codec_types else None),
        video_duration or (duration if "video" in codec_types else None),
    )


def _positive_finite_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def sample_frame_count(duration: float, interval: float) -> int:
    """Use one decimal-rational count rule for OCR progress and FFmpeg output."""
    from ..local_ocr import validate_interval

    interval = validate_interval(interval)
    duration = _positive_finite_float(duration)
    if duration is None:
        raise ValueError("视频时长无效，无法计算 OCR 抽帧数量。")
    return max(1, math.ceil(Fraction(str(duration)) / Fraction(str(interval))))


def extract_audio_chunks(path: Path, destination: Path, duration: float) -> Iterator[AudioChunk]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("未检测到 ffmpeg。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。")
    destination.mkdir(parents=True, exist_ok=True)
    total = max(1, math.ceil(duration / ASR_CHUNK_SECONDS))
    for index in range(total):
        start = index * ASR_CHUNK_SECONDS
        end = min(duration, (index + 1) * ASR_CHUNK_SECONDS)
        output = destination / f"audio-{index + 1:05d}.wav"
        try:
            result = subprocess.run(
                [
                    executable,
                    "-nostdin", "-hide_banner", "-v", "error",
                    "-protocol_whitelist", "file,pipe",
                    "-ss", f"{start:.3f}",
                    "-i", str(path),
                    "-t", f"{end - start:.3f}",
                    "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                    "-f", "wav", "-y", str(output),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(90, (end - start) * 6),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("ffmpeg 无法提取音频片段。") from None
        if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 44:
            raise ValueError("ffmpeg 未能生成有效的 ASR 音频片段。")
        if output.stat().st_size > 25 * 1000 * 1000:
            raise ValueError("ASR 音频片段超过服务商的文件大小限制。")
        yield AudioChunk(output, start, end, index + 1)


def extract_ocr_frames(path: Path, destination: Path, duration: float) -> list[OcrFrameFile]:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("未检测到 ffmpeg。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。")
    destination.mkdir(parents=True, exist_ok=True)
    count = min(MAX_OCR_FRAMES, max(1, math.ceil(duration / 30.0)))
    frames: list[OcrFrameFile] = []
    for index in range(count):
        timestamp = duration * (index + 0.5) / count
        timestamp = min(max(0.0, timestamp), max(0.0, duration - 0.05))
        output = destination / f"frame-{index + 1:02d}.jpg"
        try:
            result = subprocess.run(
                [
                    executable,
                    "-nostdin", "-hide_banner", "-v", "error",
                    "-protocol_whitelist", "file,pipe",
                    "-ss", f"{timestamp:.3f}",
                    "-i", str(path),
                    "-frames:v", "1",
                    "-vf", "scale='min(1024,iw)':-2",
                    "-q:v", "5",
                    "-y", str(output),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("ffmpeg 无法提取视频关键帧。") from None
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            raise ValueError("ffmpeg 未能生成有效的 OCR 视频帧。")
        if output.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("OCR 关键帧超过单图大小限制。")
        frame_id = f"frame-{index + 1:02d}"
        frames.append(OcrFrameFile(output, frame_id, timestamp, index + 1))
    return frames


def iter_video_samples(
    path: Path,
    destination: Path,
    duration: float,
    interval: float,
    *,
    start_index: int = 0,
    profile: str = "full",
) -> Iterator[OcrFrameFile]:
    from ..local_ocr import SUBTITLE_CROP_RATIO, validate_interval, validate_scan_profile

    interval = validate_interval(interval)
    profile = validate_scan_profile(profile)
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("未检测到 ffmpeg。请安装 FFmpeg 并将 ffmpeg.exe 加入 PATH。")
    destination.mkdir(parents=True, exist_ok=True)
    count = sample_frame_count(duration, interval)
    if isinstance(start_index, bool) or not isinstance(start_index, int) or start_index < 0:
        raise ValueError("OCR 续跑帧序号无效。")
    first_index = min(count, start_index)
    if first_index >= count:
        return
    filters = []
    if profile == "subtitles":
        crop_height = 1.0 - SUBTITLE_CROP_RATIO
        filters.append(f"crop=iw:ih*{SUBTITLE_CROP_RATIO:.3f}:0:ih*{crop_height:.3f}")
    fps_fraction = Fraction(str(interval)).limit_denominator(1_000_000)
    fps = f"{fps_fraction.denominator}/{fps_fraction.numerator}"
    filter_interval = format(interval, ".15g")
    filters.extend((
        "scale='min(1920,iw)':-2",
        f"tpad=stop_mode=clone:stop_duration={filter_interval}",
        f"fps={fps}:start_time=0:round=near",
    ))
    command = [
        executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file,pipe",
        "-i", str(path), "-an", "-sn", "-dn", "-vf", ",".join(filters),
        "-frames:v", str(count), "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
    ]
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, bufsize=64 * 1024)
    except OSError:
        raise ValueError("ffmpeg 无法启动，不能抽取视频帧。") from None

    buffer = bytearray()
    output_index = 0
    read_chunk = process.stdout.read1 if process.stdout is not None and hasattr(process.stdout, "read1") else process.stdout.read
    try:
        while True:
            chunk = read_chunk(64 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                if start < 0:
                    if len(buffer) > 1:
                        del buffer[:-1]
                    break
                end = buffer.find(b"\xff\xd9", start + 2)
                if end < 0:
                    if start:
                        del buffer[:start]
                    break
                jpeg = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                output_index += 1
                if output_index <= first_index:
                    continue
                index = output_index - 1
                output = destination / f"frame-{output_index:05d}.jpg"
                output.write_bytes(jpeg)
                sampled_at = min(index * interval, max(0.0, duration - 0.05))
                yield OcrFrameFile(output, f"frame-{output_index:05d}", sampled_at, output_index)
        return_code = process.wait(timeout=60)
        if return_code != 0:
            raise ValueError("ffmpeg 抽帧失败，请检查本地视频文件。")
        if output_index != count:
            raise ValueError(f"ffmpeg 只生成了 {output_index}/{count} 张视频帧。")
    except (OSError, subprocess.SubprocessError):
        raise ValueError("ffmpeg 无法连续抽取视频帧。") from None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process.stdout is not None:
            process.stdout.close()


def private_tempdir(prefix: str):
    """Create an automatically cleaned local temporary work directory."""
    return tempfile.TemporaryDirectory(prefix=f"workbench-{prefix}-")


__all__ = [
    "ASR_CHUNK_SECONDS",
    "AudioChunk",
    "MediaProbe",
    "OcrFrameFile",
    "extract_audio_chunks",
    "extract_ocr_frames",
    "probe_media",
    "private_tempdir",
]
