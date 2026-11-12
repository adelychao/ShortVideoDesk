"""Official single-post URL routing shared by the local multi-platform tool."""
from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from yt_dlp.dependencies import requests

from app.platforms import extract_url

PLATFORMS = {
    "douyin": "抖音", "xiaohongshu": "小红书",
    "bilibili": "B站", "weibo": "微博", "tiktok": "TikTok",
    "instagram": "Instagram", "twitter": "X / Twitter", "reddit": "Reddit",
}


@dataclass(frozen=True)
class Source:
    platform: str
    url: str
    media_id: str | None
    is_short: bool = False

    @property
    def name(self):
        return PLATFORMS[self.platform]


class DownloadIssue(ValueError):
    def __init__(self, message: str, code: str = "extraction_failed", evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def parse_source(value: str) -> Source:
    url = extract_url(value)
    p = urlsplit(url)
    # 作者：zhw；日期：2026-10-24 00:30:57。旧版小红书分享链接使用 HTTP，仅对该官方短域名升级 HTTPS。
    if p.scheme == "http" and p.hostname == "xhslink.com":
        url = urlunsplit(p._replace(scheme="https"))
        p = urlsplit(url)
    if p.scheme != "https" or p.port not in (None, 443):
        raise ValueError("只接受支持平台的官方 HTTPS 作品链接")
    if len(url) > 4096:
        raise ValueError("链接过长")
    host, path = p.hostname, p.path
    short_hosts = {
        "v.douyin.com": "douyin", "xhslink.com": "xiaohongshu",
        "b23.tv": "bilibili",
        "vm.tiktok.com": "tiktok", "vt.tiktok.com": "tiktok",
    }
    if host in short_hosts and re.fullmatch(r"/(?:[A-Za-z0-9_-]+/){0,2}[A-Za-z0-9_-]+/?", path):
        return Source(short_hosts[host], url, None, True)
    patterns = [
        ("douyin", {"www.douyin.com", "www.iesdouyin.com"}, r"/(?:share/)?video/([0-9]{1,30})/?"),
        ("xiaohongshu", {"www.xiaohongshu.com", "xiaohongshu.com"}, r"/(?:explore|discovery/item)/([0-9a-fA-F]{24})/?"),
        ("bilibili", {"www.bilibili.com", "m.bilibili.com", "bilibili.com"}, r"/video/(BV[A-Za-z0-9]{10}|av[0-9]+)/?"),
        ("weibo", {"weibo.com", "www.weibo.com"}, r"/[0-9]+/([A-Za-z0-9]+)/?"),
        ("weibo", {"m.weibo.cn"}, r"/(?:status|detail)/([A-Za-z0-9]+)/?"),
        ("tiktok", {"www.tiktok.com", "tiktok.com", "m.tiktok.com"}, r"/@[^/\s]+/video/([0-9]{1,30})/?"),
        ("instagram", {"www.instagram.com", "instagram.com"}, r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)/?"),
        ("twitter", {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}, r"/(?:[^/]+|i/web)/status/([0-9]+)(?:/video/[0-9]+)?/?"),
        ("reddit", {"www.reddit.com", "reddit.com", "old.reddit.com"}, r"/(?:r/[^/]+/|user/[^/]+/)?comments/([A-Za-z0-9]+)(?:/[^/]+)?/?"),
    ]
    for platform, hosts, pattern in patterns:
        match = re.fullmatch(pattern, path)
        if host in hosts and match:
            if platform == "bilibili" and "p" in parse_qs(p.query):
                part = parse_qs(p.query)["p"]
                if len(part) != 1 or not part[0].isdigit() or not 1 <= int(part[0]) <= 10000:
                    raise ValueError("B站分P参数必须是 1 到 10000 的整数")
            return Source(platform, url, match.group(1))
    if host in {"www.tiktok.com", "tiktok.com"} and re.fullmatch(r"/t/[A-Za-z0-9_-]+/?", path):
        return Source("tiktok", url, None, True)
    if host == "redd.it" and re.fullmatch(r"/[A-Za-z0-9]+/?", path):
        return Source("reddit", url, path.strip("/"), True)
    if host in {"www.reddit.com", "reddit.com"} and re.fullmatch(r"/r/[^/]+/s/[A-Za-z0-9]+/?", path):
        return Source("reddit", url, None, True)
    raise ValueError("链接不属于支持平台的单条作品；暂不支持主页、合集、直播或私密内容")


def resolve_source(source: Source, *, cookies: list[dict[str, object]] | None = None) -> Source:
    if not source.is_short:
        return source
    if urlsplit(source.url).hostname == "redd.it":
        return Source("reddit", f"https://www.reddit.com/comments/{source.media_id}/", source.media_id)
    current = source
    cookie_jar = None
    if cookies:
        cookie_jar = requests.cookies.RequestsCookieJar()
        for cookie in cookies:
            name, value, domain = cookie.get("name"), cookie.get("value"), cookie.get("domain")
            if not all(isinstance(item, str) and item for item in (name, value, domain)):
                continue
            if any(character in name + value + domain for character in "\r\n\t"):
                continue
            cookie_jar.set(
                name,
                value,
                domain=domain,
                path=cookie.get("path") if isinstance(cookie.get("path"), str) else "/",
                secure=cookie.get("secure") is True,
            )
    for _ in range(6):
        response = requests.get(current.url, allow_redirects=False, timeout=12, stream=True, cookies=cookie_jar)
        try:
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise DownloadIssue("短链接未返回可用的作品地址，请使用完整作品链接", "short_link_unresolved")
            target = parse_source(urljoin(current.url, response.headers.get("Location", "")))
        finally:
            response.close()
        if target.platform != source.platform:
            raise DownloadIssue("短链接跳转到了其他平台，已停止访问", "untrusted_redirect")
        if not target.is_short:
            return target
        current = target
    raise DownloadIssue("短链接跳转次数过多", "short_link_unresolved")


def ensure_identity(source: Source, info: dict):
    expected = source.media_id
    actual = {str(info.get(k) or "") for k in ("id", "display_id")}
    if source.platform == "bilibili":
        selected_id = str(info.get("id") or "")
        part = str(int(parse_qs(urlsplit(source.url).query).get("p", ["1"])[0]))
        if expected and expected.startswith("av"):
            legacy_id = f"bilibili {expected[2:]}_part{part}"
            if legacy_id in (info.get("_old_archive_ids") or []):
                return
            raise DownloadIssue("AV作品或分P身份未通过核对", "identity_mismatch")
        if re.search(r"_p[0-9]+$", selected_id):
            if selected_id == f"{expected}_p{part}":
                return
            raise DownloadIssue("解析器返回了其他分P，未下载该文件", "identity_mismatch")
        if f"{expected}_p{part}" in actual:
            return
    if expected in actual:
        return
    raise DownloadIssue("解析器返回的作品与原链接不一致，未下载推荐内容", "identity_mismatch")


def redact_url(value: str) -> str:
    p = urlsplit(value)
    query = re.sub(r"(?i)(xsec_token|token|signature|sign|auth|key)=[^&]*", r"\1=<redacted>", p.query)
    return urlunsplit(p._replace(query=query))


def classify_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, DownloadIssue):
        return exc.code, str(exc)
    from app.browser_safety import BrowserSafetyError
    if isinstance(exc, BrowserSafetyError):
        return "browser_safety_blocked", "平台页面暂时无法打开，请稍后重试。"
    text = str(exc).lower()
    code = getattr(exc, "issue_code", "")
    code = getattr(code, "value", code)
    if any(w in text for w in ("ssl", "tls", "certificate", "timed out", "timeout", "connection", "err_name_not_resolved")) or code == "network_error":
        return "network_error", "网络或 TLS 连接失败；未更改代理、证书校验或个人会话"
    if any(w in text for w in ("ip存在风险", "300012", "安全限制", "security check")) or code == "security_blocked":
        return "security_blocked", "平台限制了当前网络的匿名访问，未继续尝试绕过验证"
    if code == "rate_limited" or any(w in text for w in ("429", "rate limit", "too many requests")):
        return "rate_limited", "平台暂时限制请求频率，请稍后重试"
    if code in {"login_required", "verification_required"} or any(w in text for w in ("authentication is required", "login required", "log in", "sign in", "captcha", "cookies")):
        return "authentication_required", "平台要求登录、访客凭据或验证；当前工具仅使用匿名会话"
    if any(w in text for w in ("404", "not found", "unavailable", "removed", "deleted")):
        return "content_unavailable", "该作品不可用，可能已删除、受地区限制或不允许匿名访问"
    if "ffprobe" in text:
        if "未检测到" in text or "not found" in text or "no such file" in text:
            return "local_dependency", "未检测到 ffprobe，请安装 FFmpeg 并将 ffprobe.exe 加入 PATH"
        return "local_dependency", "无法读取视频媒体信息，请确认 ffprobe 可用且本地文件完整"
    if "ffmpeg" in text:
        if "未检测到" in text or "not found" in text or "no such file" in text:
            return "local_dependency", "未检测到 FFmpeg，请安装并将 ffmpeg.exe 加入 PATH"
        return "local_dependency", "FFmpeg 未能完整处理视频，请检查文件是否完整及其编码是否受支持"
    if "chrome" in text or "executable" in text:
        return "local_dependency", "请检查本机所需的浏览器或媒体处理依赖是否可用"
    if "403" in text:
        return "access_rejected", "平台拒绝了当前匿名请求（HTTP 403）"
    return "extraction_failed", "没有获取到完整、可验证的视频数据，可能是作品类型或站点接口变化"
