"""Submit real samples to the local API and independently check downloaded files."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from yt_dlp.dependencies import requests
from multi_sources import PLATFORMS, redact_url

CHINA = timezone(timedelta(hours=8))
TERMINAL = {"已完成", "部分完成", "失败", "已中断"}
SAMPLES = [
    {"platform": "douyin", "url": "https://v.douyin.com/bC7lNN42zBo/", "source": "用户提供的 REA 视频"},
    {"platform": "bilibili", "url": "https://www.bilibili.com/video/BV1sapm6KE2q/", "source": "按用户关键词 REA 逆向搜索到的 B站作品原页"},
    {"platform": "weibo", "url": "https://m.weibo.cn/status/4189191225395228", "source": "yt-dlp 官方提取器公开测试样本"},
    {"platform": "tiktok", "url": "https://www.tiktok.com/@patroxofficial/video/6742501081818877190", "source": "yt-dlp 官方提取器公开测试样本"},
    {"platform": "instagram", "url": "https://www.instagram.com/reel/Chunk8-jurw/", "source": "yt-dlp 官方提取器公开测试样本"},
    {"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9", "source": "yt-dlp 官方提取器公开测试样本；未检索到可验证的 REA 主题笔记链接"},
    {"platform": "twitter", "url": "https://twitter.com/captainamerica/status/719944021058060289", "source": "yt-dlp 官方提取器公开测试样本"},
    {"platform": "reddit", "url": "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/", "source": "yt-dlp 官方提取器公开测试样本"},
    {"platform": "xiaohongshu", "url": "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15?xsec_token=CBgeL8Dxd1ZWBhwqRd568gAZ_iwG-9JIf9tnApNmteU2E=", "source": "yt-dlp 官方公开样本，含公开分享参数，参数不进入报告"},
    {"platform": "bilibili", "url": "https://www.bilibili.com/video/BV1bK411W797?p=1", "source": "yt-dlp 官方分P测试样本"},
]


def check_local_artifacts(job, base_url):
    result = job["result"]
    video = Path(result["files"]["video"])
    root = (ROOT / "downloads").resolve()
    if root not in video.resolve().parents or not video.is_file():
        raise ValueError("Video file missing or outside the download directory")
    with video.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != result["metadata"]["sha256"]:
        raise ValueError("Video hash differs from saved metadata")
    probe = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,codec_name,width,height:format=duration,size", "-of", "json", str(video),
    ], capture_output=True, text=True, check=True, timeout=30)
    media = json.loads(probe.stdout)
    if not any(s.get("codec_type") == "video" for s in media.get("streams", [])):
        raise ValueError("Downloaded output is not video")
    endpoints = {}
    for kind in result["files"]:
        response = requests.get(f"{base_url}/api/jobs/{job['id']}/files/{kind}", headers={"Range": "bytes=0-31"}, timeout=10)
        endpoints[kind] = {"status": response.status_code, "content_type": response.headers.get("Content-Type"), "bytes": len(response.content)}
        if response.status_code not in {200, 206} or not response.content:
            raise ValueError(f"Local {kind} file endpoint failed")
    return {"ffprobe": media, "sha256": digest, "file_endpoints": endpoints,
            "video_path": str(video), "metadata": result["metadata"]}


def run_case(index, sample, base_url, deadline):
    start = time.monotonic()
    result = {"case": index + 1, "platform": sample["platform"], "url": redact_url(sample["url"]),
              "sample_source": sample["source"], "download_verified": False}
    stage = "submit"
    try:
        response = requests.post(base_url + "/api/jobs", json={"url": sample["url"]}, timeout=10)
        response.raise_for_status()
        job_id = response.json()["id"]
        result["job_id"] = job_id
        stage = "poll"
        while time.monotonic() - start < deadline:
            response = requests.get(base_url + "/api/jobs", timeout=10)
            response.raise_for_status()
            job = next(j for j in response.json() if j["id"] == job_id)
            if job["status"] in TERMINAL:
                result.update(status=job["status"], phase=job.get("phase"), message=job.get("message"),
                              parsed=bool(job.get("parsed") or job.get("result")),
                              error_code=job.get("error_code"), error_type=job.get("error_type"),
                              access_evidence=job.get("access_evidence", {}))
                if job.get("result"):
                    stage = "artifacts"
                    result["artifacts"] = check_local_artifacts(job, base_url)
                    result["download_verified"] = True
                break
            time.sleep(.7)
        else:
            result.update(status="验收超时", error_code="validation_timeout")
    except Exception as exc:
        result.update(status="验收错误", error_code=type(exc).__name__, validation_stage=stage,
                      diagnostic_message=re.sub(r"https?://\S+", "<url>", str(exc))[:1000])
    result["elapsed_seconds"] = round(time.monotonic() - start, 2)
    result["finished_at"] = datetime.now(CHINA).isoformat()
    print(json.dumps({k: result.get(k) for k in ["case", "platform", "status", "download_verified", "error_code", "elapsed_seconds"]}, ensure_ascii=True), flush=True)
    return result


def build_catalog(reports):
    unique = {}
    for report in sorted(reports, key=lambda r: r.get("started_at", "")):
        for case in report.get("cases", []):
            identity = case.get("job_id") or f"{report.get('started_at')}:{case.get('case')}"
            unique[identity] = {**case, "run_time": report.get("started_at", "")}
    result = {}
    for key in PLATFORMS:
        cases = [case for case in unique.values() if case.get("platform") == key]
        passed = sum(bool(case.get("download_verified")) for case in cases)
        latest = max(cases, key=lambda c: (c.get("run_time", ""), c.get("finished_at", ""), c.get("case", 0))) if cases else {}
        status = "未验证" if not cases else "已通过" if passed == len(cases) else "有波动" if passed else "受限／失败"
        result[key] = {"status": status, "passed": passed, "tested": len(cases),
                       "last_status": latest.get("status"), "tested_at": latest.get("run_time", ""),
                       "error_codes": sorted({c["error_code"] for c in cases if c.get("error_code")})}
    return result


def audit_existing(root):
    from anonymous_poc import inspect_video
    files = {}
    for report_path in root.glob("live-validation-*.json"):
        for case in json.loads(report_path.read_text(encoding="utf-8")).get("cases", []):
            artifacts = case.get("artifacts")
            if artifacts:
                record = files.setdefault(artifacts["video_path"], {
                    "path": artifacts["video_path"], "platform": case["platform"],
                    "metadata": artifacts["metadata"], "expected_hashes": set(),
                })
                record["expected_hashes"].add(artifacts["sha256"])
    def check(record):
        result = {"path": record["path"], "platform": record["platform"]}
        try:
            media = inspect_video(Path(record["path"]), record["metadata"]["duration_seconds"],
                                  require_audio=bool(record["metadata"].get("audio_codec")))
            result.update(status="passed", all_historical_hashes_match=record["expected_hashes"] == {media["sha256"]}, **media)
        except Exception as exc:
            result.update(status="failed", error_type=type(exc).__name__, message=str(exc))
        print(json.dumps({k: result.get(k) for k in ("platform", "status", "full_decode_verified", "all_historical_hashes_match")}, ensure_ascii=True), flush=True)
        return result
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(check, files.values()))
    report = {"files": results, "passed": sum(i["status"] == "passed" and i.get("all_historical_hashes_match") for i in results), "total": len(results)}
    (root / "full-decode-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] == report["total"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8767")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--case-timeout", type=int, default=180)
    parser.add_argument("--refresh-catalog", action="store_true")
    parser.add_argument("--audit-existing", action="store_true")
    args = parser.parse_args()
    if args.audit_existing:
        return audit_existing(ROOT / "data" / "validation" / "2026-10-24")
    if args.refresh_catalog:
        reports = [json.loads(p.read_text(encoding="utf-8")) for p in
                   (ROOT / "data" / "validation" / "2026-10-24").glob("live-validation-*.json")]
        catalog = build_catalog(reports)
        (ROOT / "data" / "platform-validation.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(catalog, ensure_ascii=True))
        return 0
    if args.base_url not in {f"http://127.0.0.1:{port}" for port in (8767, 8768)}:
        parser.error("Only the local validation ports are accepted")
    health = requests.get(args.base_url + "/api/health", timeout=5).json()
    if health.get("personal_chrome_cookies_used") is not False:
        raise RuntimeError("Anonymous-only health check failed")
    samples = json.loads(args.manifest.read_text(encoding="utf-8")) if args.manifest else SAMPLES
    timestamp = datetime.now(CHINA).strftime("%Y%m%d-%H%M%S")
    target = ROOT / "data" / "validation" / "2026-10-24" / f"live-validation-{timestamp}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(CHINA).isoformat(), "base_url": args.base_url,
              "personal_chrome_cookies_used": False, "cases": []}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run_case, i, sample, args.base_url, args.case_timeout) for i, sample in enumerate(samples)]
        for future in as_completed(futures):
            report["cases"].append(future.result())
            report["cases"].sort(key=lambda row: row["case"])
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    previous = [json.loads(p.read_text(encoding="utf-8")) for p in target.parent.glob("live-validation-*.json") if p != target]
    catalog = build_catalog(previous + [report])
    report.update(finished_at=datetime.now(CHINA).isoformat(), summary=catalog,
                  passed=sum(c["download_verified"] for c in report["cases"]), total=len(samples))
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (ROOT / "data" / "platform-validation.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    (target.parent / "latest-live-validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {target.name}; downloaded and verified: {report['passed']}/{report['total']}", flush=True)
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
