"""Bounded, anonymous comment-access investigation; does not change the app."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import unescape
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlsplit, urlunsplit

from yt_dlp.dependencies import requests

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "comment-research" / "2026-10-24"
CHINA = timezone(timedelta(hours=8))
PAGES = {
    "douyin": "https://www.douyin.com/video/7696880924297792803",
    "bilibili": "https://www.bilibili.com/video/BV1sapm6KE2q/",
    "weibo": "https://m.weibo.cn/status/4189191225395228",
    "tiktok": "https://www.tiktok.com/@patroxofficial/video/6742501081818877190",
    "instagram": "https://www.instagram.com/reel/Chunk8-jurw/",
    "twitter": "https://x.com/captainamerica/status/719944021058060289",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"


def clean_text(value):
    return unescape(re.sub(r"<[^>]+>", "", str(value or "")))


def endpoint(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def normalize(platform, data):
    result = []
    if not isinstance(data, dict):
        return result
    if platform == "bilibili":
        payload = data.get("data") or {}
        entries = list(payload.get("replies") or []) + list(payload.get("hots") or [])
        top = payload.get("top_replies") or []
        if isinstance(top, list):
            entries += top
        for c in entries:
            if not isinstance(c, dict):
                continue
            result.append({"id": str(c.get("rpid", "")), "text": str((c.get("content") or {}).get("message") or ""),
                           "author": (c.get("member") or {}).get("uname"), "timestamp": c.get("ctime"),
                           "like_count": c.get("like"), "parent_id": str(c.get("parent") or "") or None,
                           "root_id": str(c.get("root") or "") or None, "reply_count": c.get("rcount")})
            result.extend(normalize(platform, {"data": {"replies": c.get("replies") or []}}))
    elif platform == "weibo":
        payload = data.get("data") or []
        entries = payload.get("data") or payload.get("comments") or [] if isinstance(payload, dict) else payload
        for c in entries if isinstance(entries, list) else []:
            result.append({"id": str(c.get("id", "")), "text": clean_text(c.get("text")),
                           "author": (c.get("user") or {}).get("screen_name"), "timestamp": c.get("created_at"),
                           "like_count": c.get("like_count", c.get("like_counts")), "parent_id": c.get("reply_id"),
                           "reply_count": c.get("total_number")})
            children = c.get("comments") or []
            if isinstance(children, list):
                for child in normalize(platform, {"data": children}):
                    child["parent_id"] = str(c.get("id", ""))
                    child["root_id"] = str(c.get("id", ""))
                    result.append(child)
    elif platform == "douyin":
        for c in data.get("comments") or []:
            result.append({"id": str(c.get("cid", "")), "text": c.get("text"),
                           "author": (c.get("user") or {}).get("nickname"), "timestamp": c.get("create_time"),
                           "like_count": c.get("digg_count"), "parent_id": c.get("reply_id"),
                           "reply_count": c.get("reply_comment_total")})
    elif platform == "tiktok":
        for c in data.get("comments") or []:
            result.append({"id": str(c.get("cid", "")), "text": c.get("text"),
                           "author": (c.get("user") or {}).get("nickname"), "timestamp": c.get("create_time"),
                           "like_count": c.get("digg_count"), "parent_id": c.get("reply_id"),
                           "reply_count": c.get("reply_comment_total")})
    elif platform == "instagram":
        for c in data.get("comments") or []:
            result.append({"id": str(c.get("pk", "")), "text": c.get("text"),
                           "author": (c.get("user") or {}).get("username"), "timestamp": c.get("created_at"),
                           "like_count": c.get("comment_like_count"), "parent_id": c.get("parent_comment_id"),
                           "reply_count": c.get("child_comment_count")})
    return result


def response_summary(platform, data):
    if not isinstance(data, dict):
        return {}
    payload = data.get("data") or {}
    return {"keys": list(data)[:20], "code": data.get("code", data.get("status_code", data.get("ok"))),
            "message": data.get("message", data.get("msg", data.get("error"))),
            "page": payload.get("page") if isinstance(payload, dict) else None,
            "cursor": payload.get("cursor", data.get("cursor")) if isinstance(payload, dict) else data.get("cursor"),
            "has_more": data.get("has_more", payload.get("has_more") if isinstance(payload, dict) else None),
            "payload_info": {k: payload[k] for k in ("need_login", "control", "notice", "max_id", "max_id_type", "total_number") if isinstance(payload, dict) and k in payload},
            "media_identity": {k: payload[k] for k in ("aid", "bvid") if isinstance(payload, dict) and k in payload},
            "comments": normalize(platform, data)[:80]}


def http_probe(platform):
    records = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": UA, "Referer": PAGES[platform]})

        def fetch(label, url, params=None):
            record = {"label": label, "endpoint": endpoint(url), "params": params or {}}
            data = {}
            try:
                response = session.get(url, params=params, timeout=20)
                record.update(http_status=response.status_code, final_endpoint=endpoint(response.url),
                              content_type=response.headers.get("Content-Type"), bytes=len(response.content))
                try:
                    data = response.json()
                except ValueError:
                    record["non_json_excerpt"] = response.text[:350]
                record.update(response_summary(platform, data))
            except Exception as exc:
                record.update(error_type=type(exc).__name__, error_message=re.sub(r"https?://\S+", "<url>", str(exc))[:350])
            records.append(record)
            return data

        if platform == "bilibili":
            meta = fetch("作品元数据", "https://api.bilibili.com/x/web-interface/view", {"bvid": "BV1sapm6KE2q"})
            details = meta.get("data") or {}
            aid = details.get("aid") if details.get("bvid") == "BV1sapm6KE2q" else None
            if aid:
                first = fetch("热度第一页", "https://api.bilibili.com/x/v2/reply", {"type": 1, "oid": aid, "pn": 1, "ps": 20, "sort": 2})
                if normalize(platform, first):
                    fetch("热度第二页", "https://api.bilibili.com/x/v2/reply", {"type": 1, "oid": aid, "pn": 2, "ps": 20, "sort": 2})
                    fetch("时间第一页", "https://api.bilibili.com/x/v2/reply", {"type": 1, "oid": aid, "pn": 1, "ps": 20, "sort": 0})
                    root = next((c for c in (first.get("data") or {}).get("replies", []) if c.get("rcount")), None)
                    if root:
                        fetch("楼中楼第一页", "https://api.bilibili.com/x/v2/reply/reply", {"type": 1, "oid": aid, "root": root["rpid"], "pn": 1, "ps": 20})
            fetch("既有分P样本评论", "https://api.bilibili.com/x/v2/reply", {"type": 1, "oid": 498159642, "pn": 1, "ps": 20, "sort": 2})
        elif platform == "douyin":
            fetch("匿名评论首屏", "https://www.douyin.com/aweme/v1/web/comment/list/", {"device_platform": "webapp", "aid": 6383, "aweme_id": "7696880924297792803", "cursor": 0, "count": 20, "item_type": 0})
        elif platform == "weibo":
            fetch("移动页热门评论", "https://m.weibo.cn/comments/hotflow", {"id": "4189191225395228", "mid": "4189191225395228", "max_id_type": 0})
            fetch("移动页评论列表", "https://m.weibo.cn/api/comments/show", {"id": "4189191225395228", "page": 1})
        elif platform == "tiktok":
            fetch("匿名评论首屏", "https://www.tiktok.com/api/comment/list/", {"aweme_id": "6742501081818877190", "count": 20, "cursor": 0})
        elif platform == "instagram":
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            pk = 0
            for char in "Chunk8-jurw":
                pk = pk * 64 + alphabet.index(char)
            fetch("匿名媒体评论", f"https://i.instagram.com/api/v1/media/{pk}/comments/", {"can_support_threading": "true", "permalink_enabled": "false"})
        elif platform == "twitter":
            fetch("官方搜索无授权基线", "https://api.x.com/2/tweets/search/recent", {"query": "conversation_id:719944021058060289", "tweet.fields": "created_at,public_metrics,referenced_tweets", "max_results": 10})
    result = {"platform": platform, "mode": "anonymous_http", "tested_at": datetime.now(CHINA).isoformat(), "source_url": PAGES[platform], "personal_cookies_used": False, "requests": records}
    save(platform + "-http", result)
    return result


def save(name, value):
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / f"{name}.json"
    attempts = OUTPUT / "attempts"
    attempts.mkdir(exist_ok=True)
    timestamp = datetime.now(CHINA).strftime("%H%M%S-%f")
    if target.exists():
        (attempts / f"{name}-before-{timestamp}.json").write_bytes(target.read_bytes())
    serialized = json.dumps(value, ensure_ascii=False, indent=2)
    target.write_text(serialized, encoding="utf-8")
    (attempts / f"{name}-{timestamp}.json").write_text(serialized, encoding="utf-8")
    count = len(value.get("comments") or []) + sum(len(r.get("comments") or []) for r in value.get("requests", []))
    print(json.dumps({"saved": name, "platform": value.get("platform"), "comment_records": count}, ensure_ascii=True), flush=True)


def browser_probe(platform, *, followup=False):
    from playwright.sync_api import sync_playwright
    records = []
    report = {"platform": platform, "mode": "fresh_anonymous_browser", "tested_at": datetime.now(CHINA).isoformat(), "source_url": PAGES[platform], "personal_cookies_used": False, "requests": records}
    last_payload = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            context = browser.new_context(locale="zh-CN", viewport={"width": 1365, "height": 1000})
            page = context.new_page()
            page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"media", "image", "font"} else route.continue_())

            def response_received(response):
                path = urlsplit(response.url).path
                is_comment = bool(re.search(r"comment|/reply|TweetDetail|TweetResult", path, re.I))
                if not is_comment:
                    return
                record = {"endpoint": endpoint(response.url), "http_status": response.status, "trigger": "normal_page"}
                params = parse_qs(urlsplit(response.url).query)
                record["request_binding"] = {k: params[k] for k in ("oid", "type", "id", "mid", "aweme_id", "cursor", "count", "mode", "next", "root", "comment_id", "pn", "sort") if k in params}
                try:
                    data = response.json()
                    record.update(response_summary(platform, data))
                    if isinstance(data, dict):
                        last_payload[path] = data
                except Exception:
                    record["json_unavailable"] = True
                records.append(record)

            page.on("response", response_received)
            try:
                response = page.goto(PAGES[platform], wait_until="domcontentloaded", timeout=30000)
                report["page_status"] = response.status if response else None
                page.wait_for_timeout(6000)
                if platform == "bilibili":
                    page.evaluate("window.scrollTo(0, 1000)")
                elif platform in {"douyin", "tiktok"}:
                    for selector in ('[data-e2e="comment-icon"]', '[data-e2e="browse-comment"]', '[data-e2e="comment-count"]'):
                        target = page.locator(selector).first
                        if target.is_visible():
                            target.click(timeout=2500)
                            report["comment_trigger"] = selector
                            break
                    page.evaluate("window.scrollTo(0, 800)")
                else:
                    page.evaluate("window.scrollTo(0, 700)")
                page.wait_for_timeout(6000)
                if followup:
                    report["followup_actions"] = []
                    if platform == "douyin":
                        for label in ("点击加载更多", "展开1条回复"):
                            button = page.get_by_role("button", name=label, exact=True).first
                            if button.is_visible():
                                button.click(timeout=4000)
                                report["followup_actions"].append(label)
                                page.wait_for_timeout(3500)
                    elif platform == "weibo":
                        first = last_payload.get("/comments/hotflow") or {}
                        body = first.get("data") or {}
                        params = {"id": "4189191225395228", "mid": "4189191225395228", "max_id": body.get("max_id"), "max_id_type": body.get("max_id_type", 0)}
                        if params["max_id"]:
                            r = context.request.get("https://m.weibo.cn/comments/hotflow", params=params, timeout=15000)
                            record = {"endpoint": "https://m.weibo.cn/comments/hotflow", "http_status": r.status, "trigger": "returned_cursor_page_2"}
                            try:
                                record.update(response_summary(platform, r.json()))
                            except ValueError:
                                record["non_json_excerpt"] = r.text()[:350]
                            records.append(record)
                            report["followup_actions"].append("returned_cursor_page_2")
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(2000)
                    elif platform == "bilibili":
                        for label in ("最新评论", "最新"):
                            target = page.get_by_text(label, exact=True).first
                            if target.is_visible():
                                target.click(timeout=4000)
                                report["followup_actions"].append(label)
                                page.wait_for_timeout(3500)
                                break
                        report["comment_shadow_text"] = page.locator("bili-comments").evaluate_all("els => els.map(e => e.shadowRoot?.innerText || e.innerText)")
                    elif platform == "tiktok":
                        targets = page.locator('[data-e2e="browse-comment-icon"], [data-e2e="comment-icon"], [data-e2e="browse-comment-count"]')
                        report["comment_targets"] = targets.count()
                        for i in range(min(targets.count(), 3)):
                            if targets.nth(i).is_visible():
                                targets.nth(i).click(timeout=4000)
                                report["followup_actions"].append("open_visible_comment_panel")
                                page.wait_for_timeout(3500)
                                break
                body_text = page.locator("body").inner_text(timeout=5000)
                report.update(final_url=endpoint(page.url), title=page.title(), body_excerpt=body_text[:9000], body_tail=body_text[-1600:])
                report["comment_dom"] = page.locator('[data-e2e*=comment]').evaluate_all("els => els.slice(0,18).map(e => ({tag:e.tagName,e2e:e.getAttribute('data-e2e'),text:e.innerText.slice(0,80)}))")
                if platform == "bilibili":
                    report["page_media_identity"] = page.evaluate("({aid:window.__INITIAL_STATE__?.aid || window.__INITIAL_STATE__?.videoData?.aid,bvid:window.__INITIAL_STATE__?.bvid || window.__INITIAL_STATE__?.videoData?.bvid})")
                page.screenshot(path=str(OUTPUT / f"{platform}-browser{'-followup' if followup else ''}.png"), full_page=False)
                report["comment_element_counts"] = {s: page.locator(s).count() for s in ("[data-e2e*=comment]", "bili-comments", "[data-testid=tweet]")}
                report["action_candidates"] = page.locator("button, [role=button]").evaluate_all("els => els.map(e => ({text:e.innerText, label:e.getAttribute('aria-label'), e2e:e.getAttribute('data-e2e')})).filter(x => x.text || x.label).slice(0,70)")
            except Exception as exc:
                report["error_type"] = type(exc).__name__
                report["error_message"] = re.sub(r"https?://\S+", "<url>", str(exc))[:500]
        finally:
            browser.close()
    save(platform + "-browser" + ("-followup" if followup else ""), report)
    return report


def extractor_probe(platform):
    from yt_dlp import YoutubeDL
    if platform != "instagram":
        raise ValueError("extractor 模式本轮只用于 Instagram")
    diagnostics = []

    class Logger:
        def debug(self, message): pass
        def info(self, message): pass
        def warning(self, message):
            diagnostics.append(re.sub(r"https?://\S+", "<url>", message)[:500])
        error = warning

    result = {"platform": platform, "method": "yt_dlp_public_extractor", "source_url": PAGES[platform],
              "tested_at": datetime.now(CHINA).isoformat(), "personal_cookies_used": False,
              "diagnostics": diagnostics, "all_comments_captured": False}
    try:
        with YoutubeDL({"skip_download": True, "noplaylist": True, "socket_timeout": 20,
                       "retries": 0, "extractor_retries": 0, "logger": Logger(), "getcomments": True}) as ydl:
            info = ydl.extract_info(PAGES[platform], download=False)
        if info.get("id") != "Chunk8-jurw":
            raise ValueError("Instagram 返回作品与目标链接不一致")
        comments = info.get("comments") or []
        result.update(media_id=info["id"], platform_reported_comment_count=info.get("comment_count"),
                      returned_count=len(comments), comments=[{k: c.get(k) for k in (
                          "id", "author", "author_id", "text", "like_count", "timestamp")} for c in comments[:50]])
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error_message=re.sub(r"https?://\S+", "<url>", str(exc))[:800])
    save("instagram-fresh-extractor", result)
    return result


def export_snapshots():
    def read(name):
        return json.loads((OUTPUT / (name + ".json")).read_text(encoding="utf-8"))

    snapshots = {}
    for platform in ("douyin", "bilibili", "weibo", "instagram"):
        sources = []
        comments = {}
        scope = "partial"
        total = None
        if platform == "instagram":
            data = read("instagram-fresh-extractor")
            assert data["media_id"] == "Chunk8-jurw"
            sources.append("instagram-fresh-extractor.json")
            total = data.get("platform_reported_comment_count")
            selected = data["comments"]
        else:
            data = read(platform + "-browser-followup")
            sources.append(platform + "-browser-followup.json")
            selected = []
            for record in data["requests"]:
                binding = record.get("request_binding") or {}
                target = {"douyin": ("aweme_id", "7696880924297792803"),
                          "bilibili": ("oid", "117439740248973"),
                          "weibo": ("id", "4189191225395228")}[platform]
                if binding.get(target[0]) != [target[1]]:
                    continue
                selected.extend(record.get("comments") or [])
                if platform == "bilibili":
                    assert data.get("page_media_identity") == {"aid": 117439740248973, "bvid": "BV1sapm6KE2q"}
                    total = (record.get("cursor") or {}).get("all_count", total)
                elif platform == "weibo":
                    total = (record.get("payload_info") or {}).get("total_number", total)
            if platform == "bilibili":
                legacy = read("bilibili-http")
                sources.append("bilibili-http.json")
                for record in legacy["requests"]:
                    params = record.get("params") or {}
                    if str(params.get("oid")) == "117439740248973":
                        selected.extend(record.get("comments") or [])
        for comment in selected:
            item = dict(comment)
            item["id"] = str(item["id"])
            for field in ("parent_id", "root_id"):
                value = item.get(field)
                item[field] = None if value in (None, "", 0, "0", "root") else str(value)
            stamp = item.get("timestamp")
            if isinstance(stamp, str):
                stamp = int(datetime.strptime(stamp, "%a %b %d %H:%M:%S %z %Y").timestamp())
            item["timestamp"] = stamp
            item["created_at"] = datetime.fromtimestamp(stamp, CHINA).isoformat() if stamp is not None else None
            item["like_count"] = int(item["like_count"]) if item.get("like_count") is not None else None
            assert item["text"] is not None and item["id"]
            comments[item["id"]] = item
        items = list(comments.values())
        if not items:
            raise ValueError(f"{platform} 本次证据没有有效评论，不覆盖既有样本导出")
        replies = sum(c["parent_id"] is not None for c in items)
        snapshots[platform] = {
            "platform": platform, "source_url": PAGES[platform], "exported_at": datetime.now(CHINA).isoformat(),
            "scope": scope, "all_comments_captured": False, "platform_reported_comment_count": total,
            "collected_count": len(items), "top_level_count": len(items) - replies if platform != "instagram" else None,
            "reply_count_collected": replies if platform != "instagram" else None,
            "comments_without_like_count": sum(c.get("like_count") is None for c in items),
            "evidence_files": sources, "personal_cookies_used": False, "comments": items,
        }
        folder = OUTPUT / "exports" / platform
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "comments.json").write_text(json.dumps(snapshots[platform], ensure_ascii=False, indent=2), encoding="utf-8")
        from html import escape
        for order, key in (("likes", "like_count"), ("newest", "timestamp")):
            ordered = sorted(items, key=lambda c: (c.get(key) is not None, c.get(key) or 0), reverse=True)
            lines = [f"# {platform} 评论研究样本", "", f"来源：{PAGES[platform]}", "",
                     f"采集数：{len(items)}。范围：部分可访问评论，未取得全量。", "",
                     f"排序：本次已采集样本按{'点赞数' if order == 'likes' else '时间'}降序；不是平台全局排名。", ""]
            lines.extend(["缺失的点赞数标为未获取，按赞排序时置于末尾，不填为 0。", ""])
            for i, c in enumerate(ordered, 1):
                like_text = "未获取" if c.get("like_count") is None else str(c["like_count"])
                lines.extend([f"## {i}. {escape(c.get('author') or '未获取作者')}", "",
                              f"评论 ID：{c['id']}；点赞：{like_text}；发布时间：{c.get('created_at')}", "",
                              f"父评论 ID：{c.get('parent_id') or '未提供／主评论'}", "",
                              *["> " + escape(line) for line in str(c["text"]).splitlines()], ""])
            (folder / f"comments-{order}.md").write_text("\n".join(lines), encoding="utf-8")
    summary = {platform: {k: v for k, v in snapshot.items() if k != "comments"} for platform, snapshot in snapshots.items()}
    (OUTPUT / "export-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v["collected_count"] for k, v in summary.items()}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("http", "browser", "extractor"), default="http")
    parser.add_argument("--platform", choices=PAGES, action="append")
    parser.add_argument("--followup", action="store_true")
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if args.export:
        export_snapshots()
        return
    probe = {"http": http_probe, "browser": lambda platform: browser_probe(platform, followup=args.followup),
             "extractor": extractor_probe}[args.mode]
    selected_platforms = args.platform or (["instagram"] if args.mode == "extractor" else list(PAGES))
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(probe, selected_platforms))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
