"""Publishing account settings and adapters for supported article platforms."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Iterator

from .article_capabilities import article_platform_capabilities
from .article_studio import ARTICLE_PLATFORMS
from .config import AppConfig
from .credentials import CredentialStoreUnavailable, get_article_platform_secret, set_article_platform_secret, delete_article_platform_secret
from .database import Database, PROJECT_ROOT
from .image_storage import resolve_article_image


_SETTINGS_KEY = "article_platform_accounts"
_WECHAT_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_WECHAT_TOKEN_LOCK = threading.Lock()
_WEIBO_LOGIN_LOCK = threading.Lock()
_WEIBO_LOGIN_PROCESS: subprocess.Popen[str] | None = None
_MANUAL_URLS = {
    "csdn": "https://editor.csdn.net/md/",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
    "toutiao": "https://mp.toutiao.com/profile_v4/graphic/publish",
    "baijiahao": "https://baijiahao.baidu.com/builder/rc/edit",
}


class PlatformResponseError(RuntimeError):
    def __init__(self, message: str, error_code: int | str | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class PlatformConnectionError(RuntimeError):
    pass


def platform_statuses(
    config: AppConfig,
    *,
    include_live_status: bool = True,
) -> list[dict[str, Any]]:
    saved = _read_account_config(config)
    output: list[dict[str, Any]] = []
    for item in ARTICLE_PLATFORMS:
        platform_id = item["id"]
        status: dict[str, Any] = {
            **item, "configured": False, "connected": False, "message": "",
            "authorization_url": None, "manual_url": _MANUAL_URLS.get(platform_id),
            "secret_available": True, "public_config": {}, "accounts": [],
            "capabilities": article_platform_capabilities(platform_id),
        }
        if platform_id in {"wechat", "zhihu"}:
            key_name = "app_id" if platform_id == "wechat" else "app_key"
            accounts: list[dict[str, Any]] = []
            for record in _platform_account_records(saved, platform_id):
                account_id = str(record["id"])
                public_value = str(record.get(key_name, ""))
                try:
                    secret = get_article_platform_secret(platform_id, "app_secret", None if account_id == "legacy" else account_id)
                    secret_ready = bool(secret)
                except CredentialStoreUnavailable:
                    secret_ready = False
                    status["secret_available"] = False
                accounts.append({
                    "id": account_id,
                    "name": str(record.get("name") or ("默认公众号" if platform_id == "wechat" else "默认知乎账号")),
                    "credential_verified_at": str(record.get("credential_verified_at") or "") or None,
                    "credential_verified": bool(record.get("credential_verified_at")),
                    "public_value": public_value,
                    "configured": bool(public_value and secret_ready),
                    "secret_available": status["secret_available"],
                })
            configured = any(account["configured"] for account in accounts)
            connected = any(account["configured"] and account["credential_verified"] for account in accounts)
            public_value = next((str(record.get(key_name, "")) for record in _platform_account_records(saved, platform_id) if record.get(key_name)), "")
            status.update({
                "configured": configured, "connected": connected, "accounts": accounts,
                "public_config": {key_name: public_value} if public_value else {},
                "authorization_url": "https://mp.weixin.qq.com/cgi-bin/loginpage" if platform_id == "wechat" else "https://www.zhihu.com/playground/zhihu-publisher",
            })
            if platform_id == "wechat":
                status["message"] = "AppID 与 AppSecret 已通过微信接口验证；发布权限仍取决于账号资质和 IP 白名单。当前直连接口不提供公众号自身头像读取。" if connected else "添加 AppID 和 AppSecret 后，系统会验证调用凭据；Secret 仅保存在 Windows 凭据管理器。当前直连接口不提供公众号自身头像读取。"
            else:
                status["message"] = "知乎 OpenAPI 仅向获准创作者开放；凭据已保存不代表账号已有发布权限。" if configured else "添加知乎主页标识 / App Key 与开放平台申请的 App Secret。"
        elif platform_id == "weibo":
            status.update(_weibo_status(include_live_status=include_live_status))
        elif platform_id == "toutiao":
            status["message"] = "今日头条文章将通过当前已连接账号浏览器发表；发表权限、平台选项与审核状态以实时页面为准。"
        else:
            status["message"] = "当前提供平台编辑页入口和手动发布；暂不调用未经核实的文章接口。"
        output.append(status)
    return output


def save_platform_account(
    config: AppConfig, platform_id: str, *, public_value: str,
    secret_value: str | None, clear_secret: bool = False,
) -> dict[str, Any]:
    if platform_id not in {"wechat", "zhihu"}:
        raise ValueError("该平台的账号连接方式不支持在此设置。")
    clean = public_value.strip()
    if not clean or len(clean) > 256:
        raise ValueError("请填写有效的平台应用标识。")
    if platform_id == "wechat" and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", clean):
        raise ValueError("公众号 AppID 格式无效。")
    if platform_id == "zhihu" and not re.fullmatch(r"[A-Za-z0-9._-]{2,128}", clean):
        raise ValueError("知乎 App Key 格式无效。")
    key_name = "app_id" if platform_id == "wechat" else "app_key"
    saved = _read_account_config(config)
    current = saved.get(platform_id, {})
    if isinstance(current, dict) and isinstance(current.get("accounts"), list):
        records = _platform_account_records(saved, platform_id)
        legacy = next((record for record in records if str(record.get("id")) == "legacy"), None)
        if legacy is None and any(str(record.get(key_name, "")).casefold() == clean.casefold() for record in records):
            raise ValueError("这个平台账号已经添加，请在账号列表中编辑对应账号。")
        current_public_value = str(legacy.get(key_name, "")) if legacy else ""
        if clean != current_public_value and not clear_secret and not (isinstance(secret_value, str) and secret_value.strip()):
            raise ValueError("更换应用标识时，请同时提供对应的新 App Secret。")
        if platform_id == "wechat" and (clean != current_public_value or clear_secret or (isinstance(secret_value, str) and secret_value.strip())):
            _invalidate_saved_wechat_credential("legacy", current_public_value)
            if legacy is not None:
                legacy.pop("credential_verified_at", None)
        if clear_secret:
            delete_article_platform_secret(platform_id, "app_secret")
        elif isinstance(secret_value, str) and secret_value.strip():
            set_article_platform_secret(secret_value, platform_id, "app_secret")
        if legacy is None:
            legacy = {"id": "legacy", "name": "默认公众号" if platform_id == "wechat" else "默认知乎账号", key_name: clean}
            records.append(legacy)
        else:
            legacy[key_name] = clean
        _save_platform_account_records(config, platform_id, records)
        return next(item for item in platform_statuses(config) if item["id"] == platform_id)
    current_public_value = str(current.get(key_name, "")) if isinstance(current, dict) else ""
    if clean != current_public_value and not clear_secret and not (isinstance(secret_value, str) and secret_value.strip()):
        raise ValueError("更换应用标识时，请同时提供对应的新 App Secret。")
    if platform_id == "wechat" and (clean != current_public_value or clear_secret or (isinstance(secret_value, str) and secret_value.strip())):
        _invalidate_saved_wechat_credential("legacy", current_public_value)
    if clear_secret:
        delete_article_platform_secret(platform_id, "app_secret")
    elif isinstance(secret_value, str) and secret_value.strip():
        set_article_platform_secret(secret_value, platform_id, "app_secret")
    with config.database.transaction(write=True) as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_SETTINGS_KEY,)).fetchone()
        try:
            values = json.loads(str(row["value_json"])) if row is not None else {}
        except (TypeError, json.JSONDecodeError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        values[platform_id] = {key_name: clean}
        connection.execute(
            """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
            (_SETTINGS_KEY, json.dumps(values, ensure_ascii=False, separators=(",", ":")), _utc_now()),
        )
    return next(item for item in platform_statuses(config) if item["id"] == platform_id)


def add_platform_account(
    config: AppConfig,
    platform_id: str,
    *,
    account_name: str,
    public_value: str,
    secret_value: str,
    credential_verified_at: str | None = None,
) -> dict[str, Any]:
    key_name = _platform_key_name(platform_id)
    clean_public = _validate_public_value(platform_id, public_value)
    clean_name = _validate_account_name(account_name)
    if not isinstance(secret_value, str) or not secret_value.strip():
        raise ValueError("请填写该账号的 App Secret。")
    account_id = uuid.uuid4().hex
    records = _platform_account_records(_read_account_config(config), platform_id)
    if any(str(item.get(key_name, "")).casefold() == clean_public.casefold() for item in records):
        raise ValueError("这个平台账号已经添加。")
    set_article_platform_secret(secret_value, platform_id, "app_secret", account_id)
    entry = {"id": account_id, "name": clean_name, key_name: clean_public}
    if platform_id == "wechat" and credential_verified_at:
        entry["credential_verified_at"] = credential_verified_at
    try:
        records.append(entry)
        _save_platform_account_records(config, platform_id, records)
    except BaseException:
        delete_article_platform_secret(platform_id, "app_secret", account_id)
        raise
    return next(item for item in platform_statuses(config) if item["id"] == platform_id)


def update_platform_account(
    config: AppConfig,
    platform_id: str,
    account_id: str,
    *,
    account_name: str,
    public_value: str,
    secret_value: str | None,
    credential_verified_at: str | None = None,
) -> dict[str, Any]:
    key_name = _platform_key_name(platform_id)
    legacy_account = account_id == "legacy"
    if not legacy_account and not re.fullmatch(r"[a-f0-9]{32}", account_id):
        raise ValueError("平台账号标识无效。")
    clean_public = _validate_public_value(platform_id, public_value)
    clean_name = _validate_account_name(account_name)
    records = _platform_account_records(_read_account_config(config), platform_id)
    current = next((record for record in records if str(record.get("id")) == account_id), None)
    if current is None:
        raise KeyError("平台账号不存在。")
    if any(str(item.get("id")) != account_id and str(item.get(key_name, "")).casefold() == clean_public.casefold() for item in records):
        raise ValueError("这个平台账号已经添加。")
    if clean_public != str(current.get(key_name, "")) and not (isinstance(secret_value, str) and secret_value.strip()):
        raise ValueError("更换平台标识时，请同时提供对应的新 App Secret。")
    if platform_id == "wechat" and (clean_public != str(current.get(key_name, "")) or (isinstance(secret_value, str) and secret_value.strip())):
        _invalidate_saved_wechat_credential(account_id, str(current.get(key_name, "")))
    if isinstance(secret_value, str) and secret_value.strip():
        set_article_platform_secret(secret_value, platform_id, "app_secret", None if legacy_account else account_id)
    current.update({"name": clean_name, key_name: clean_public})
    if platform_id == "wechat":
        if credential_verified_at:
            current["credential_verified_at"] = credential_verified_at
        else:
            current.pop("credential_verified_at", None)
    _save_platform_account_records(config, platform_id, records)
    return next(item for item in platform_statuses(config) if item["id"] == platform_id)


def remove_platform_account(config: AppConfig, platform_id: str, account_id: str) -> dict[str, Any]:
    _platform_key_name(platform_id)
    records = _platform_account_records(_read_account_config(config), platform_id)
    target = next((record for record in records if str(record.get("id")) == account_id), None)
    if target is None:
        raise KeyError("平台账号不存在。")
    if platform_id == "wechat":
        _invalidate_saved_wechat_credential(account_id, str(target.get("app_id", "")))
    if account_id == "legacy":
        delete_article_platform_secret(platform_id, "app_secret")
    else:
        delete_article_platform_secret(platform_id, "app_secret", account_id)
    _save_platform_account_records(config, platform_id, [record for record in records if str(record.get("id")) != account_id])
    return next(item for item in platform_statuses(config) if item["id"] == platform_id)


def account_credentials(config: AppConfig, platform_id: str, account_id: str | None) -> tuple[str, str]:
    key_name = _platform_key_name(platform_id)
    records = _platform_account_records(_read_account_config(config), platform_id)
    target_id = account_id or "legacy"
    record = next((item for item in records if str(item.get("id")) == target_id), None)
    if record is None and account_id is None:
        record = next((item for item in records if item.get(key_name)), None)
        target_id = str(record.get("id")) if record is not None else ""
    if record is None:
        raise RuntimeError("请先在「账号管理 → 文章发布」添加并选择该平台账号。")
    secret = get_article_platform_secret(platform_id, "app_secret", None if target_id == "legacy" else target_id)
    public_value = str(record.get(key_name, ""))
    if not public_value or not secret:
        raise RuntimeError("该平台账号凭据不完整；请检查 App ID / App Key 和 Secret。")
    return public_value, secret


def verify_wechat_credentials(app_id: str, app_secret: str) -> str:
    clean_app_id = _validate_public_value("wechat", app_id)
    if not isinstance(app_secret, str) or not app_secret.strip():
        raise ValueError("请填写公众号 AppSecret。")
    clean_secret = app_secret.strip()
    _invalidate_wechat_access_token(clean_app_id, clean_secret)
    _wechat_access_token(clean_app_id, clean_secret)
    return _utc_now()


def _wechat_token_key(app_id: str, app_secret: str) -> str:
    secret_fingerprint = hashlib.sha256(app_secret.encode("utf-8")).hexdigest()
    return f"{app_id}:{secret_fingerprint}"


def _invalidate_wechat_access_token(app_id: str, app_secret: str | None = None) -> None:
    prefix = f"{app_id}:"
    with _WECHAT_TOKEN_LOCK:
        if app_secret:
            _WECHAT_TOKEN_CACHE.pop(_wechat_token_key(app_id, app_secret), None)
        else:
            for key in tuple(_WECHAT_TOKEN_CACHE):
                if key.startswith(prefix):
                    _WECHAT_TOKEN_CACHE.pop(key, None)


def _wechat_access_token(app_id: str, app_secret: str) -> str:
    cache_key = _wechat_token_key(app_id, app_secret)
    now = time.monotonic()
    with _WECHAT_TOKEN_LOCK:
        cached = _WECHAT_TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]
        _WECHAT_TOKEN_CACHE.pop(cache_key, None)
        response = _json_request(
            "https://api.weixin.qq.com/cgi-bin/token?"
            + urllib.parse.urlencode({
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": app_secret,
            }),
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise PlatformResponseError(_platform_error(response, "公众号没有返回 Access Token。"), response.get("errcode"))
        try:
            expires_in = int(response.get("expires_in", 7200))
        except (TypeError, ValueError):
            expires_in = 7200
        _WECHAT_TOKEN_CACHE[cache_key] = (token, time.monotonic() + max(1, expires_in - 300))
        return token


def verify_wechat_account_credentials(config: AppConfig, account_id: str) -> dict[str, Any]:
    app_id, app_secret = account_credentials(config, "wechat", account_id)
    saved = _read_account_config(config)
    records = _platform_account_records(saved, "wechat")
    target = next((record for record in records if str(record.get("id")) == account_id), None)
    if target is None:
        raise KeyError("公众号账号不存在。")
    _invalidate_wechat_access_token(app_id, app_secret)
    _wechat_access_token(app_id, app_secret)
    target["credential_verified_at"] = _utc_now()
    _save_platform_account_records(config, "wechat", records)
    return next(item for item in platform_statuses(config) if item["id"] == "wechat")


def _invalidate_saved_wechat_credential(account_id: str, app_id: str) -> None:
    if not app_id:
        return
    try:
        secret = get_article_platform_secret(
            "wechat", "app_secret", None if account_id == "legacy" else account_id,
        )
    except CredentialStoreUnavailable:
        secret = None
    _invalidate_wechat_access_token(app_id, secret)


def _wechat_response(request: urllib.request.Request, app_id: str, app_secret: str) -> dict[str, Any]:
    try:
        return _read_json_response(request)
    except PlatformResponseError as exc:
        try:
            error_code = int(exc.error_code) if exc.error_code is not None else None
        except (TypeError, ValueError):
            error_code = None
        if error_code in {40001, 40014, 42001}:
            _invalidate_wechat_access_token(app_id, app_secret)
        raise


def _wechat_json_request(
    app_id: str,
    app_secret: str,
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=raw,
        headers={"Accept": "application/json", **({"Content-Type": "application/json; charset=utf-8"} if raw else {})},
        method=method,
    )
    return _wechat_response(request, app_id, app_secret)


def publish_article(
    config: AppConfig,
    platform_id: str,
    document: dict[str, Any],
    *,
    account_id: str | None = None,
) -> dict[str, Any]:
    document = {**document, "_image_database": config.database}
    if platform_id == "wechat":
        return _publish_wechat_draft(config, document, account_id=account_id)
    if platform_id == "zhihu":
        return _publish_zhihu(config, document, account_id=account_id)
    if platform_id == "weibo":
        return _publish_weibo(document)
    raise ValueError("该平台当前仅支持手动发布。")


def prepare_csdn_publication(document: dict[str, Any], tags: list[str]) -> dict[str, Any]:
    cleaned_tags = [tag.strip() for tag in tags if isinstance(tag, str) and tag.strip()]
    if not 1 <= len(cleaned_tags) <= 50 or len(cleaned_tags) != len(tags):
        raise ValueError("请选择有效的 CSDN 文章标签，数量以平台实际限制为准。")
    if any(len(tag) > 100 for tag in cleaned_tags) or len(set(cleaned_tags)) != len(cleaned_tags):
        raise ValueError("CSDN 标签过长或重复，请重新选择。")
    title = str(document.get("title") or "").strip()
    body = str(document.get("body_markdown") or "").strip()
    if not title or not body:
        raise ValueError("CSDN 文章标题和正文不能为空。")
    return {"title": title, "body_markdown": body, "tags": cleaned_tags}


def _platform_key_name(platform_id: str) -> str:
    if platform_id not in {"wechat", "zhihu"}:
        raise ValueError("该平台的账号连接方式不支持在此设置。")
    return "app_id" if platform_id == "wechat" else "app_key"


def _validate_public_value(platform_id: str, public_value: str) -> str:
    clean = public_value.strip()
    if not clean or len(clean) > 256:
        raise ValueError("请填写有效的平台应用标识。")
    if platform_id == "wechat" and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", clean):
        raise ValueError("公众号 AppID 格式无效。")
    if platform_id == "zhihu" and not re.fullmatch(r"[A-Za-z0-9._-]{2,128}", clean):
        raise ValueError("知乎 App Key 格式无效。")
    return clean


def _validate_account_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80:
        raise ValueError("账号名称须为 1 到 80 个字符。")
    return name


def _platform_account_records(saved: dict[str, Any], platform_id: str) -> list[dict[str, Any]]:
    value = saved.get(platform_id)
    if not isinstance(value, dict):
        return []
    accounts = value.get("accounts")
    if isinstance(accounts, list):
        return [dict(item) for item in accounts if isinstance(item, dict) and isinstance(item.get("id"), str)]
    key_name = "app_id" if platform_id == "wechat" else "app_key"
    if isinstance(value.get(key_name), str) and value[key_name].strip():
        name = "默认公众号" if platform_id == "wechat" else "默认知乎账号"
        return [{"id": "legacy", "name": name, key_name: value[key_name]}]
    return []


def _save_platform_account_records(config: AppConfig, platform_id: str, records: list[dict[str, Any]]) -> None:
    key_name = _platform_key_name(platform_id)
    with config.database.transaction(write=True) as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_SETTINGS_KEY,)).fetchone()
        try:
            values = json.loads(str(row["value_json"])) if row is not None else {}
        except (TypeError, json.JSONDecodeError):
            values = {}
        if not isinstance(values, dict):
            values = {}
        values[platform_id] = {"accounts": [
            {
                "id": str(item["id"]),
                "name": str(item["name"]),
                key_name: str(item[key_name]),
                **{
                    field: item[field]
                    for field in ("credential_verified_at",)
                    if isinstance(item.get(field), str)
                },
            }
            for item in records
        ]}
        connection.execute(
            """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at""",
            (_SETTINGS_KEY, json.dumps(values, ensure_ascii=False, separators=(",", ":")), _utc_now()),
        )


def _read_account_config(config: AppConfig) -> dict[str, Any]:
    with config.database.transaction() as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (_SETTINGS_KEY,)).fetchone()
    if row is None:
        return {}
    try:
        value = json.loads(str(row["value_json"]))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _weibo_status(*, include_live_status: bool = True) -> dict[str, Any]:
    executable = _weibo_cli_executable()
    login_in_progress = _weibo_login_active()
    if not executable:
        return {
            "configured": False, "connected": False, "cli_installed": False,
            "login_in_progress": False, "publish_ready": False,
            "connection_checked": False, "readiness_checked": False,
            "developer_verified": False, "service_ready": False,
            "message": "请安装 Node.js 18+ 并运行 npm install -g @weibo-ai/weibo-cli；安装不会自动执行。",
            "authorization_url": "https://open.weibo.com/cli/quickstart",
            "manual_url": "https://weibo.com/",
        }
    if not include_live_status:
        return {
            "configured": True, "connected": False, "cli_installed": True,
            "login_in_progress": login_in_progress, "publish_ready": False,
            "connection_checked": False, "readiness_checked": False,
            "developer_verified": False, "service_ready": False,
            "connection_checking": True,
            "message": "正在检查微博账号与发布准备状态。",
            "authorization_url": "https://open.weibo.com/cli/quickstart",
            "manual_url": "https://weibo.com/",
        }
    try:
        result = _run_weibo(["auth", "whoami", "--output", "json"], timeout=12)
    except (OSError, subprocess.TimeoutExpired):
        return {
            "configured": True, "connected": False, "cli_installed": True,
            "login_in_progress": login_in_progress, "publish_ready": False,
            "connection_checked": False, "readiness_checked": False,
            "developer_verified": False, "service_ready": False,
            "message": "微博官方 CLI 暂时无法确认账号连接；请检查 CLI 和网络后刷新状态。",
            "authorization_url": "https://open.weibo.com/cli/quickstart",
            "manual_url": "https://weibo.com/",
        }
    if result.returncode != 0:
        message = (
            "微博浏览器授权正在进行；完成后点击“检查连接”。"
            if login_in_progress
            else "微博官方 CLI 未能确认账号连接状态；请检查 CLI 或网络后刷新。如尚未登录，再启动登录。"
        )
        return {
            "configured": True, "connected": False, "cli_installed": True,
            "login_in_progress": login_in_progress, "publish_ready": False,
            "connection_checked": False, "readiness_checked": False,
            "developer_verified": False, "service_ready": False, "message": message,
            "authorization_url": "https://open.weibo.com/cli/quickstart",
            "manual_url": "https://weibo.com/",
        }
    value = _parse_json(result.stdout)
    user = value.get("user") if isinstance(value.get("user"), dict) else {}
    nickname = str(user.get("username") or "").strip()[:100]
    if not nickname:
        return {
            "configured": True, "connected": False, "cli_installed": True,
            "login_in_progress": login_in_progress, "publish_ready": False,
            "connection_checked": False, "readiness_checked": False,
            "developer_verified": False, "service_ready": False,
            "message": "微博官方 CLI 的账号响应无法识别，暂时无法确认账号连接；请检查 CLI 版本后刷新。",
            "authorization_url": "https://open.weibo.com/cli/quickstart",
            "manual_url": "https://weibo.com/",
        }
    try:
        readiness_result = _run_weibo(["doctor", "--output", "json"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        readiness_result = None
    readiness = _parse_json(readiness_result.stdout) if readiness_result and readiness_result.returncode == 0 else {}
    steps = readiness.get("steps") if isinstance(readiness.get("steps"), dict) else {}
    developer_verified = steps.get("developer_verification") is True
    service_ready = steps.get("service") is True
    readiness_checked = (
        readiness_result is not None
        and readiness_result.returncode == 0
        and isinstance(readiness.get("ready"), bool)
        and isinstance(steps.get("developer_verification"), bool)
        and isinstance(steps.get("service"), bool)
    )
    publish_ready = readiness_checked and readiness.get("ready") is True and developer_verified and service_ready
    urls = readiness.get("urls") if isinstance(readiness.get("urls"), dict) else {}
    service_url = _weibo_official_url(urls.get("plan"))
    developer_url = _weibo_official_url(urls.get("developer_verification"))
    if publish_ready:
        message = f"已连接微博账号：{nickname}。官方 CLI 检查显示账号已完成认证并开通服务。"
    elif not readiness_checked:
        message = f"已连接微博账号：{nickname}。暂未确认微博发布准备状态；请检查 CLI 或网络后刷新。"
    else:
        missing = []
        if not developer_verified:
            missing.append("开发者认证")
        if not service_ready:
            missing.append("开通体验或正式服务")
        message = f"已连接微博账号：{nickname}。请先完成{'和'.join(missing) or '官方发布准备检查'}，再发布文章。"
    return {
        "configured": True, "connected": True, "cli_installed": True,
        "login_in_progress": login_in_progress, "publish_ready": publish_ready,
        "connection_checked": True, "readiness_checked": readiness_checked,
        "developer_verified": developer_verified, "service_ready": service_ready,
        "name": nickname, "message": message,
        "authorization_url": "https://open.weibo.com/cli/quickstart",
        "developer_url": developer_url, "service_url": service_url,
        "manual_url": "https://weibo.com/",
    }


def _weibo_cli_executable() -> str | None:
    return shutil.which("weibo-cli") or shutil.which("weibo") or shutil.which("wb")


def _weibo_official_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.hostname == "open.weibo.com":
        return candidate
    return None


def _weibo_article_url(value: Any) -> str | None:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except ValueError:
        return None
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme == "https" and (hostname == "weibo.com" or hostname.endswith(".weibo.com")):
        return candidate
    return None


def _weibo_login_active() -> bool:
    global _WEIBO_LOGIN_PROCESS
    with _WEIBO_LOGIN_LOCK:
        if _WEIBO_LOGIN_PROCESS is None:
            return False
        if _WEIBO_LOGIN_PROCESS.poll() is None:
            return True
        _WEIBO_LOGIN_PROCESS = None
        return False


def start_weibo_login() -> dict[str, Any]:
    global _WEIBO_LOGIN_PROCESS
    executable = _weibo_cli_executable()
    if not executable:
        raise FileNotFoundError("请先安装微博官方 CLI。")
    if os.name != "nt":
        raise PlatformConnectionError("微博浏览器登录需要在 Windows 桌面启动；请在本机终端运行 weibo auth login --browser。")
    with _WEIBO_LOGIN_LOCK:
        if _WEIBO_LOGIN_PROCESS is not None and _WEIBO_LOGIN_PROCESS.poll() is None:
            raise PlatformConnectionError("微博授权窗口正在进行，请完成或关闭当前授权后重试。")
        _WEIBO_LOGIN_PROCESS = None
        command = _weibo_command(executable, ["auth", "login", "--browser"])
        try:
            _WEIBO_LOGIN_PROCESS = subprocess.Popen(
                command, stdin=None, stdout=None, stderr=None, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010),
            )
        except OSError:
            raise PlatformConnectionError("无法启动微博官方登录窗口；请检查 CLI 安装和 Windows 权限。") from None
    return {"started": True, "message": "已打开微博官方登录窗口。完成浏览器授权后，返回此页点击“检查连接”。"}


def _publish_wechat_draft(config: AppConfig, document: dict[str, Any], *, account_id: str | None = None) -> dict[str, Any]:
    try:
        app_id, app_secret = account_credentials(config, "wechat", account_id)
    except CredentialStoreUnavailable as exc:
        raise RuntimeError(str(exc)) from None
    token = _wechat_access_token(app_id, app_secret)
    media_id = _wechat_create_draft(app_id, app_secret, token, document)
    try:
        submitted = _wechat_json_request(
            app_id, app_secret,
            "https://api.weixin.qq.com/cgi-bin/freepublish/submit?access_token=" + urllib.parse.quote(token),
            method="POST", body={"media_id": media_id},
        )
    except PlatformResponseError as exc:
        error_code = str(exc.error_code or "")
        if error_code == "48001":
            reason = "微信未授权调用发布接口（48001：接口权限未开通）。请在微信公众平台检查账号认证状态与发布接口权限；也可以先在后台手动发表。"
        elif error_code == "40164":
            reason = "微信拒绝本次调用（40164：请求出口 IP 不在白名单）。请把程序访问微信 API 时使用的公网 IP 加入公众号 IP 白名单后再试。"
        else:
            reason = f"微信明确拒绝提交发布：{_safe_error(str(exc))}"
        return {
            "status": "failed", "remote_id": media_id, "remote_url": None,
            "message": f"公众号草稿已创建并保留在草稿箱，但本次未发表（草稿编号 {media_id}）。{reason} 微信返回：{_safe_error(str(exc))}",
        }
    except PlatformConnectionError as exc:
        return {
            "status": "unknown", "remote_id": None, "remote_url": None,
            "message": f"公众号草稿已创建，但提交结果因网络中断无法确认（草稿编号 {media_id}）：{_safe_error(str(exc))}请先在公众号后台核对，不要直接重发。",
        }
    except RuntimeError as exc:
        return {
            "status": "unknown", "remote_id": None, "remote_url": None,
            "message": f"公众号草稿已创建，但提交发布结果不明确（草稿编号 {media_id}）：{_safe_error(str(exc))}请先在公众号后台核对，不要直接重发。",
        }
    publish_id = submitted.get("publish_id")
    if not isinstance(publish_id, str) or not publish_id:
        return {
            "status": "unknown", "remote_id": None, "remote_url": None,
            "message": f"公众号已响应提交，但未返回发布任务编号（草稿编号 {media_id}）。请先在公众号后台核对，不要直接重发。",
        }
    return {"status": "publishing", "remote_id": publish_id, "remote_url": None, "message": f"微信已受理发布任务，正在等待平台审核与发布结果。发布任务：{publish_id}"}


def _wechat_create_draft(app_id: str, app_secret: str, token: str, document: dict[str, Any]) -> str:
    cover = _safe_article_cover(document)
    if cover is None:
        raise RuntimeError("公众号图文消息需要封面图；请先上传文章封面。")
    thumb_id = _wechat_upload_cover(token, cover, app_id, app_secret)
    content_markdown, body_image_urls = _wechat_prepare_body_images(
        document, token, str(document.get("body_markdown") or ""), app_id, app_secret,
    )
    content_html = markdown_to_html(content_markdown)
    for marker, image_url in body_image_urls:
        content_html = content_html.replace(marker, f'<img src="{html.escape(image_url, quote=True)}" />')
    article = {
        "title": str(document.get("title") or "未命名文章"),
        "author": str(document.get("author") or ""),
        "digest": str(document.get("summary") or "")[:120],
        "content": content_html,
        "content_source_url": "", "thumb_media_id": thumb_id, "show_cover_pic": 1,
        "need_open_comment": 1, "only_fans_can_comment": 0,
    }
    result = _wechat_json_request(
        app_id, app_secret,
        "https://api.weixin.qq.com/cgi-bin/draft/add?access_token=" + urllib.parse.quote(token),
        method="POST", body={"articles": [article]},
    )
    media_id = result.get("media_id")
    if not isinstance(media_id, str) or not media_id:
        raise RuntimeError("公众号没有返回草稿编号。")
    return media_id


def send_wechat_fan_notification(
    config: AppConfig,
    account_id: str,
    publication_id: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    try:
        app_id, app_secret = account_credentials(config, "wechat", account_id)
    except CredentialStoreUnavailable as exc:
        return {"status": "failed", "remote_id": None, "message": str(exc)}
    token = _wechat_access_token(app_id, app_secret)
    media_id = _wechat_create_draft(app_id, app_secret, token, document)
    client_message_id = hashlib.sha256(
        f"{account_id}:{publication_id}:fan-notification".encode("utf-8")
    ).hexdigest()[:32]
    try:
        result = _wechat_json_request(
            app_id, app_secret,
            "https://api.weixin.qq.com/cgi-bin/message/mass/sendall?access_token=" + urllib.parse.quote(token),
            method="POST",
            body={
                "filter": {"is_to_all": True},
                "mpnews": {"media_id": media_id},
                "msgtype": "mpnews",
                "clientmsgid": client_message_id,
                "send_ignore_reprint": 0,
            },
        )
    except PlatformResponseError as exc:
        code = str(exc.error_code or "")
        if code == "48001":
            reason = "微信未授权调用群发接口（48001）。请在开发者中心检查群发接口权限和公众号认证资质。"
        elif code == "40164":
            reason = "请求出口 IP 不在公众号 IP 白名单（40164）。请加入实际调用微信 API 的公网出口 IP。"
        else:
            reason = f"微信拒绝本次群发：{_safe_error(str(exc))}"
        return {"status": "failed", "remote_id": None, "message": reason}
    except PlatformConnectionError as exc:
        return {
            "status": "unknown", "remote_id": None,
            "message": f"群发提交结果因网络中断无法确认。请检查公众号后台后再处理，不要直接重发：{_safe_error(str(exc))}",
        }
    except RuntimeError as exc:
        return {
            "status": "unknown", "remote_id": None,
            "message": f"群发接口响应异常，结果待核实。请检查公众号后台后再处理，不要直接重发：{_safe_error(str(exc))}",
        }
    remote_id = result.get("msg_id")
    if not isinstance(remote_id, (str, int)) or not str(remote_id):
        return {"status": "unknown", "remote_id": None, "message": "微信未返回群发消息 ID，提交结果待核实；请先检查公众号后台，不要直接重发。"}
    return {
        "status": "sending", "remote_id": str(remote_id),
        "message": f"微信已受理全体粉丝群发任务，正在查询发送结果。消息 ID：{remote_id}",
    }


def refresh_wechat_fan_notification(config: AppConfig, account_id: str, message_id: str) -> dict[str, Any]:
    try:
        app_id, app_secret = account_credentials(config, "wechat", account_id)
    except CredentialStoreUnavailable as exc:
        return {"status": "unknown", "remote_id": message_id, "message": str(exc)}
    try:
        token = _wechat_access_token(app_id, app_secret)
        response = _wechat_json_request(
            app_id, app_secret,
            "https://api.weixin.qq.com/cgi-bin/message/mass/get?access_token=" + urllib.parse.quote(token),
            method="POST", body={"msg_id": message_id},
        )
    except PlatformConnectionError as exc:
        return {"status": "sending", "remote_id": message_id, "message": f"网络暂时不可用，群发仍待查询：{_safe_error(str(exc))}"}
    except PlatformResponseError as exc:
        code = str(exc.error_code or "")
        if code in {"89504", "89505"}:
            message = "公众号已开启 API 群发安全保护，请管理员在微信后台确认；未确认或拒绝会导致群发失败。"
            return {"status": "sending", "remote_id": message_id, "message": message}
        return {
            "status": "unknown", "remote_id": message_id,
            "message": f"微信暂时无法确认粉丝通知结果，请在公众号后台核对后再刷新：{_safe_error(str(exc))}",
        }
    except RuntimeError as exc:
        return {"status": "unknown", "remote_id": message_id, "message": f"群发结果查询异常，请到公众号后台核对：{_safe_error(str(exc))}"}
    message_status = str(response.get("msg_status") or "").upper()
    if message_status == "SEND_SUCCESS":
        return {"status": "sent", "remote_id": message_id, "message": "微信已确认粉丝群发成功。"}
    if message_status in {"SEND_FAIL", "DELETE"}:
        return {"status": "failed", "remote_id": message_id, "message": "微信确认该粉丝群发失败或已删除，请查看公众号后台详情。"}
    return {"status": "sending", "remote_id": message_id, "message": "微信群发任务仍在执行，系统会继续查询结果。"}


def refresh_wechat_publication(config: AppConfig, account_id: str, publish_id: str) -> dict[str, Any]:
    try:
        app_id, app_secret = account_credentials(config, "wechat", account_id)
    except CredentialStoreUnavailable as exc:
        raise RuntimeError(str(exc)) from None
    try:
        token = _wechat_access_token(app_id, app_secret)
        response = _wechat_json_request(
            app_id, app_secret,
            "https://api.weixin.qq.com/cgi-bin/freepublish/get?access_token=" + urllib.parse.quote(token),
            method="POST", body={"publish_id": publish_id},
        )
    except PlatformConnectionError as exc:
        return {"status": "publishing", "remote_id": publish_id, "remote_url": None, "message": f"网络暂时不可用，稍后可继续查询公众号状态：{_safe_error(str(exc))}"}
    except PlatformResponseError as exc:
        return {"status": "unknown", "remote_id": publish_id, "remote_url": None, "message": f"微信接口暂时无法确认此任务状态。请检查账号凭据、接口权限与 IP 白名单后刷新；如仍失败，请到公众号后台核对。{_safe_error(str(exc))}"}
    except RuntimeError as exc:
        return {"status": "unknown", "remote_id": publish_id, "remote_url": None, "message": f"暂时无法确认公众号任务状态。请检查账号设置并到公众号后台核对后重试：{_safe_error(str(exc))}"}
    try:
        publish_status = int(response.get("publish_status"))
    except (TypeError, ValueError):
        return {"status": "publishing", "remote_id": publish_id, "remote_url": None, "message": "公众号暂未返回最终发布状态，系统会继续等待。"}
    if publish_status == 0:
        details = response.get("article_detail") if isinstance(response.get("article_detail"), dict) else {}
        items = details.get("item") if isinstance(details.get("item"), list) else []
        first = items[0] if items and isinstance(items[0], dict) else {}
        article_id = response.get("article_id")
        article_url = first.get("article_url")
        return {"status": "published", "remote_id": str(article_id or publish_id), "remote_url": str(article_url) if article_url else None, "message": "公众号已确认文章发布成功。"}
    if publish_status == 1:
        return {"status": "publishing", "remote_id": publish_id, "remote_url": None, "message": "微信已受理，文章仍在发布或审核中。"}
    messages = {
        2: "公众号原创声明校验未通过。", 3: "公众号发布失败，请查看公众号后台错误原因。",
        4: "公众号审核未通过，请在后台修改后重新发布。", 5: "文章发布后已被删除。",
        6: "文章发布后已被平台封禁。",
    }
    return {"status": "failed", "remote_id": publish_id, "remote_url": None, "message": messages.get(publish_status, f"公众号返回未知发布状态：{publish_status}。")}


def _publish_zhihu(config: AppConfig, document: dict[str, Any], *, account_id: str | None = None) -> dict[str, Any]:
    try:
        app_key, app_secret = account_credentials(config, "zhihu", account_id)
    except CredentialStoreUnavailable as exc:
        raise RuntimeError(str(exc)) from None
    timestamp, log_id, extra_info = str(int(time.time())), uuid.uuid4().hex, ""
    sign_source = f"app_key:{app_key}|ts:{timestamp}|logid:{log_id}|extra_info:{extra_info}"
    signature = base64.b64encode(hmac.new(app_secret.encode(), sign_source.encode(), hashlib.sha256).digest()).decode("ascii")
    body_markdown, image_replacements, image_count, has_cover = _zhihu_prepare_body_images(document, app_key, app_secret)
    content_html = markdown_to_html(body_markdown)
    for marker, image_html in image_replacements:
        content_html = content_html.replace(marker, image_html)
    result = _json_request(
        "https://openapi.zhihu.com/openapi/publish", method="POST",
        body={
            "type": "article",
            "confirmed": True,
            "confirm_note": "confirmed by user after local preview",
            "content": {
                "title": str(document.get("title") or "未命名文章"),
                "html": content_html,
                "comment_permission": "all",
                "table_of_contents_enabled": False,
            },
        },
        headers={"X-App-Key": app_key, "X-Timestamp": timestamp, "X-Log-Id": log_id, "X-Extra-Info": extra_info, "X-Sign": signature},
    )
    if result.get("status") != 0:
        raise RuntimeError(_safe_error(str(result.get("msg") or "知乎发布接口没有确认发布成功。")) or "知乎发布失败。")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    remote_id, remote_url = data.get("content_token"), data.get("url")
    image_note = f"已上传并嵌入 {image_count} 张正文配图。" if image_count else ""
    cover_note = "封面图已作为正文首图上传嵌入（知乎文章接口未提供独立封面字段）。" if has_cover else ""
    return {
        "status": "published", "remote_id": str(remote_id) if remote_id else None,
        "remote_url": str(remote_url) if remote_url else None,
        "message": f"知乎 OpenAPI 已接受这篇文章。{image_note}{cover_note}".strip(),
    }


def _replace_managed_article_image_reference(
    markdown: str, article_id: str, block: dict[str, Any], marker: str,
) -> tuple[str, bool]:
    block_id = str(block.get("id") or "")
    if not re.fullmatch(r"[\w.-]{1,128}", block_id):
        return markdown, False

    image_prefix = f"/api/v1/articles/{article_id}/images/"
    found = False
    image_pattern = re.compile(
        r'''!\[(?:\\.|[^\]])*\]\(\s*(?:<(?P<angle>[^>]+)>|(?P<plain>[^\s)]+))'''
        r'''(?:\s+(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|\((?:\\.|[^)])*\)))?\s*\)'''
    )

    def replace(match: re.Match[str]) -> str:
        nonlocal found
        source = match.group("angle") or match.group("plain") or ""
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith(image_prefix):
            return match.group(0)
        filename = parsed.path[len(image_prefix):]
        if not re.fullmatch(r"[0-9a-f]{32}\.(?:jpe?g|png|webp)", filename):
            return match.group(0)
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if query != {"slot": [block_id]}:
            return match.group(0)
        found = True
        return marker

    replaced = image_pattern.sub(replace, markdown)
    return replaced, found


def _zhihu_prepare_body_images(
    document: dict[str, Any], app_key: str, app_secret: str,
) -> tuple[str, list[tuple[str, str]], int, bool]:
    article_id = str(document.get("_article_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", article_id):
        return str(document.get("body_markdown") or ""), [], 0, False

    body = str(document.get("body_markdown") or "")
    image_blocks = [
        block for block in document.get("blocks", [])
        if isinstance(block, dict) and isinstance(block.get("image_path"), str) and block["image_path"]
    ] if isinstance(document.get("blocks"), list) else []
    has_cover_path = bool(document.get("cover_image_path"))
    cover = _safe_article_cover(document)
    if has_cover_path and cover is None:
        raise RuntimeError("文章封面文件不可用；请重新生成或上传封面后再发布。")
    if not cover and not image_blocks:
        return body, [], 0, False

    try:
        from mediacloud_uploader import MediaCloudUploader
    except ImportError:
        raise RuntimeError("知乎配图上传组件未安装；请更新项目依赖并重启工作台后重试。") from None

    uploader = MediaCloudUploader(app_key=app_key, app_secret=app_secret)
    output = body
    replacements: list[tuple[str, str]] = []
    search_from = 0
    image_count = 0

    def upload_image(path: Path) -> str:
        nonlocal image_count
        try:
            uploaded = uploader.upload_image(file_path=str(path), scene_name="article")
        except Exception as exc:
            message = _safe_error(str(exc)).replace(app_secret, "[已隐藏]")
            raise RuntimeError(f"知乎图片上传失败：{message or '请检查知乎开放平台凭据和媒体上传权限。'}") from None
        if not uploaded.is_success or uploaded.media_type != "image":
            raise RuntimeError("知乎媒体云未确认图片上传成功；文章尚未发布。")
        media_url = uploaded.media_url
        original_url = str(getattr(media_url, "primary", "") or "")
        if not original_url.startswith("https://"):
            raise RuntimeError("知乎媒体云没有返回可用的图片地址；文章尚未发布。")
        extra = uploaded.extra
        watermark_url = str(getattr(getattr(extra, "watermark_image_url", None), "primary", "") or "")
        watermark_key = str(getattr(extra, "watermark_image_key", "") or "")
        media_key = str(getattr(uploaded, "media_key", "") or "")
        is_watermarked = bool(watermark_key and media_key and watermark_key != media_key and watermark_url.startswith("https://"))
        display_url = watermark_url if is_watermarked else original_url
        meta = uploaded.media_meta
        width = int(getattr(meta, "width", 0) or 0)
        height = int(getattr(meta, "height", 0) or 0)
        size = "normal" if width >= 343 else "small"
        attributes = {
            "src": display_url,
            "data-caption": "",
            "data-size": size,
            "data-watermark": "watermark" if is_watermarked else "",
            "data-original-src": original_url,
            "data-watermark-src": watermark_url if is_watermarked else "",
            "data-private-watermark-src": "",
        }
        if width > 0:
            attributes["data-rawwidth"] = str(width)
        if height > 0:
            attributes["data-rawheight"] = str(height)
        image_count += 1
        serialized = " ".join(f'{name}="{html.escape(value, quote=True)}"' for name, value in attributes.items())
        return f"<img {serialized} />"

    if cover:
        marker = "ARTICLE_COVER_" + uuid.uuid4().hex
        replacements.append((marker, upload_image(cover)))
        output = marker + ("\n\n" + output if output else "")
        search_from = len(marker) + 2

    for block in image_blocks:
        path = _safe_article_image(article_id, str(block["image_path"]), document.get("_image_database"))
        if path is None:
            raise RuntimeError("正文配图文件不可用；请重新生成或上传该配图后再发布。")
        marker = "ARTICLE_IMAGE_" + uuid.uuid4().hex
        output, inline_image_found = _replace_managed_article_image_reference(output, article_id, block, marker)
        if not inline_image_found:
            block_text = str(block.get("text") or "").strip()
            position = output.find(block_text, search_from) if block_text else -1
            if position >= 0:
                end = position + len(block_text)
                output = output[:end] + "\n\n" + marker + "\n\n" + output[end:]
                search_from = end + len(marker) + 4
            else:
                output = output.rstrip() + "\n\n" + marker
                search_from = len(output)
        replacements.append((marker, upload_image(path)))
    return output, replacements, len(image_blocks), cover is not None


def _publish_weibo(document: dict[str, Any]) -> dict[str, Any]:
    title, content = str(document.get("title") or "").strip(), str(document.get("body_markdown") or "").strip()
    if not title or not content:
        raise RuntimeError("微博长文需要标题和正文。")
    if len(title) + len(content) > 28_000:
        raise RuntimeError("正文超出 Windows CLI 单次命令长度限制；请分段或改用手动发布。")
    try:
        result = _run_weibo(["article", "publish", "--title", title, "--content", content, "--ai-generated", "true", "--output", "json"], timeout=90)
    except subprocess.TimeoutExpired:
        return {
            "status": "unknown", "remote_id": None, "remote_url": None,
            "message": "微博官方 CLI 发布超时，提交结果尚未确认；请先核对微博账号中的文章状态，再决定是否重试。",
        }
    if result.returncode != 0:
        detail = _safe_error(result.stderr or result.stdout)
        message = "微博官方 CLI 未确认发布结果；请先核对微博账号中的文章状态，再决定是否重试。"
        if detail:
            message += f" CLI 提示：{detail}"
        return {"status": "unknown", "remote_id": None, "remote_url": None, "message": message}
    value = _parse_json(result.stdout)
    outcome = str(value.get("outcome") or "").strip().casefold()
    receipt = value.get("result") if isinstance(value.get("result"), dict) else value
    remote_id = receipt.get("id") or receipt.get("idstr") or receipt.get("article_id")
    remote_url = _weibo_article_url(receipt.get("url"))
    if outcome and outcome != "success":
        return {
            "status": "unknown", "remote_id": str(remote_id) if remote_id else None,
            "remote_url": remote_url,
            "message": "微博 CLI 未确认发布成功；请先到微博后台核对，再决定是否重试。",
        }
    if not remote_id and not remote_url:
        return {
            "status": "unknown", "remote_id": None, "remote_url": None,
            "message": "微博 CLI 未返回可核验的文章编号或链接；请先到微博后台核对，再决定是否重试。",
        }
    return {
        "status": "published", "remote_id": str(remote_id) if remote_id else None,
        "remote_url": str(remote_url) if remote_url else None,
        "message": "微博开放平台 CLI 已返回文章回执；请在微博账号中核对文章。",
    }


def _wechat_upload_cover(token: str, path: Path, app_id: str, app_secret: str) -> str:
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.casefold(), "application/octet-stream")
    boundary = "----ArticleCover" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"{path.name.replace(chr(34), '')}\"\r\nContent-Type: {mime}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request("https://api.weixin.qq.com/cgi-bin/material/add_material?" + urllib.parse.urlencode({"access_token": token, "type": "image"}), data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    result = _wechat_response(request, app_id, app_secret)
    media_id = result.get("media_id")
    if not isinstance(media_id, str) or not media_id:
        raise RuntimeError(_platform_error(result, "公众号没有接受封面图片。"))
    return media_id


def _wechat_prepare_body_images(
    document: dict[str, Any], token: str, markdown: str, app_id: str, app_secret: str,
) -> tuple[str, list[tuple[str, str]]]:
    article_id = str(document.get("_article_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", article_id):
        return markdown, []
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        return markdown, []
    output = markdown
    replacements: list[tuple[str, str]] = []
    search_from = 0
    for block in blocks:
        if not isinstance(block, dict) or not isinstance(block.get("image_path"), str) or not block["image_path"]:
            continue
        path = _safe_article_image(article_id, str(block["image_path"]), document.get("_image_database"))
        if path is None:
            raise RuntimeError("正文配图文件不可用；请重新生成或上传该配图后再发布。")
        upload_path = path
        temporary_path: Path | None = None
        try:
            if path.suffix.casefold() not in {".jpg", ".jpeg", ".png"} or path.stat().st_size >= 950_000:
                temporary_path = _wechat_compress_body_image(path)
                upload_path = temporary_path
            image_url = _wechat_upload_body_image(token, upload_path, app_id, app_secret)
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
        marker = "ARTICLE_IMAGE_" + uuid.uuid4().hex
        output, inline_image_found = _replace_managed_article_image_reference(output, article_id, block, marker)
        if not inline_image_found:
            block_text = str(block.get("text") or "").strip()
            position = output.find(block_text, search_from) if block_text else -1
            if position >= 0:
                end = position + len(block_text)
                output = output[:end] + "\n\n" + marker + "\n\n" + output[end:]
                search_from = end + len(marker) + 4
            else:
                output = output.rstrip() + "\n\n" + marker
                search_from = len(output)
        replacements.append((marker, image_url))
    return output, replacements


def _wechat_compress_body_image(path: Path) -> Path:
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("当前环境缺少图片压缩组件；请更新项目依赖并重启工作台后重试。") from None
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            for max_edge in (2048, 1600, 1200, 1000, 800):
                candidate = image.copy()
                candidate.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                for quality in (88, 82, 76, 70, 64, 58, 52):
                    buffer = tempfile.SpooledTemporaryFile(max_size=1_000_000)
                    candidate.save(buffer, format="JPEG", optimize=True, quality=quality, progressive=True)
                    buffer.seek(0, os.SEEK_END)
                    size = buffer.tell()
                    if size < 950_000:
                        buffer.seek(0)
                        with tempfile.NamedTemporaryFile(prefix="article-wechat-", suffix=".jpg", delete=False) as target:
                            target.write(buffer.read())
                            temporary_path = Path(target.name)
                        buffer.close()
                        return temporary_path
                    buffer.close()
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("公众号正文配图自动压缩失败；请更换图片后重试。") from None
    raise RuntimeError("公众号正文配图无法压缩到接口限制内；请使用较小尺寸的图片。")


def _wechat_upload_body_image(token: str, path: Path, app_id: str, app_secret: str) -> str:
    boundary = "----ArticleBodyImage" + uuid.uuid4().hex
    safe_filename = path.name.replace('"', "")
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"{safe_filename}\"\r\nContent-Type: {('image/jpeg' if path.suffix.casefold() in {'.jpg', '.jpeg'} else 'image/png')}\r\n\r\n").encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        "https://api.weixin.qq.com/cgi-bin/media/uploadimg?" + urllib.parse.urlencode({"access_token": token}),
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    result = _wechat_response(request, app_id, app_secret)
    image_url = result.get("url")
    if not isinstance(image_url, str) or not image_url.startswith("https://"):
        raise RuntimeError(_platform_error(result, "公众号没有接受正文配图。"))
    return image_url


def _safe_article_image(article_id: str, relative: str, database: Database | None = None) -> Path | None:
    return resolve_article_image(database, article_id, relative, project_root=PROJECT_ROOT)


def xiaohongshu_article_images(
    article_id: str, document: dict[str, Any], *, database: Database | None = None,
) -> list[Path]:
    """Return the article cover and inline images in note order, after path validation."""
    candidates: list[Path | None] = []
    cover = document.get("cover_image_path")
    if isinstance(cover, str) and cover:
        candidates.append(_safe_article_image(article_id, cover, database))
    blocks = document.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if isinstance(block, dict) and isinstance(block.get("image_path"), str):
                candidates.append(_safe_article_image(article_id, str(block["image_path"]), database))
    output: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None or candidate in seen:
            continue
        seen.add(candidate)
        output.append(candidate)
    if len(output) > 18:
        raise ValueError("小红书图文最多使用 18 张图片，请减少文章封面或正文配图。")
    return output


def xiaohongshu_article_note_text(document: dict[str, Any]) -> str:
    """Flatten article Markdown to a readable Xiaohongshu note caption."""
    text = str(document.get("note") or document.get("body_markdown") or "")
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\|", "", text, flags=re.MULTILINE)
    text = re.sub(r"\|\s*$", "", text, flags=re.MULTILINE)
    text = text.replace("|", "  ")
    text = re.sub(r"[*_~`]+", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


@contextmanager
def prepare_zhihu_article_file(
    article_id: str, document: dict[str, Any], *, database: Database | None = None,
) -> Iterator[Path]:
    """Create a temporary Markdown source with only validated local article images."""
    if not re.fullmatch(r"[0-9a-f]{32}", article_id):
        raise ValueError("知乎文章标识无效。")
    with tempfile.TemporaryDirectory(prefix="workbench-zhihu-article-") as temporary_dir:
        root = Path(temporary_dir)
        image_dir = root / "images"
        markdown = str(document.get("body_markdown") or "")
        blocks = document.get("blocks")
        image_paths: dict[str, str] = {}
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                relative = block.get("image_path")
                if not isinstance(relative, str) or not relative:
                    continue
                source = _safe_article_image(article_id, relative, database)
                if source is None:
                    raise RuntimeError("知乎文章正文配图不可用；请重新上传图片后再发布。")
                image_dir.mkdir(exist_ok=True)
                target_name = source.name
                target = image_dir / target_name
                if relative not in image_paths:
                    shutil.copyfile(source, target)
                    image_paths[relative] = f"images/{target_name}"
        for source, target in image_paths.items():
            markdown = markdown.replace(f"]({source})", f"]({target})")
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or not isinstance(block.get("image_path"), str):
                    continue
                source = _safe_article_image(article_id, str(block["image_path"]), database)
                if source is None:
                    continue
                marker = "ARTICLE_LOCAL_IMAGE_" + uuid.uuid4().hex
                markdown, found = _replace_managed_article_image_reference(markdown, article_id, block, marker)
                if found:
                    markdown = markdown.replace(marker, f"![正文配图](images/{source.name})")
        for image in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", markdown):
            target = image.group(1)
            if target in image_paths.values():
                continue
            if target.startswith("https://"):
                continue
            raise RuntimeError("知乎文章正文包含未关联的本地配图；请重新保存文章后再发布。")
        content_file = root / "article.md"
        content_file.write_text(markdown, encoding="utf-8")
        yield content_file


def _json_request(url: str, *, method: str = "GET", body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    request = urllib.request.Request(url, data=raw, headers={"Accept": "application/json", **({"Content-Type": "application/json; charset=utf-8"} if raw else {}), **(headers or {})}, method=method)
    return _read_json_response(request)


def _read_json_response(request: urllib.request.Request) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            value = json.loads(response.read(2_000_001).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or exc.code >= 500:
            raise PlatformConnectionError("平台服务暂时不可用，请稍后再试。") from None
        try:
            value = json.loads(exc.read(100_000).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PlatformConnectionError("平台接口返回了无法识别的 HTTP 错误，请稍后再试。") from None
        if not isinstance(value, dict):
            raise PlatformConnectionError("平台接口返回了无法识别的 HTTP 错误，请稍后再试。") from None
        raise PlatformResponseError(_platform_error(value, "平台接口拒绝了本次发布。"), value.get("errcode")) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlatformConnectionError("无法连接平台发布接口；请检查网络后再试。") from None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise PlatformConnectionError("平台返回了无法识别的响应，请稍后再试。") from None
    if not isinstance(value, dict):
        raise PlatformConnectionError("平台返回了无法识别的响应，请稍后再试。")
    if value.get("errcode") not in (None, 0, "0") or value.get("success") is False or value.get("error"):
        raise PlatformResponseError(_platform_error(value, "平台接口拒绝了本次发布。"), value.get("errcode") or value.get("code"))
    return value


def _safe_article_cover(document: dict[str, Any]) -> Path | None:
    relative = document.get("cover_image_path")
    if not isinstance(relative, str) or not relative:
        return None
    database = document.get("_image_database")
    article_id = str(document.get("_article_id") or "")
    if database is not None and re.fullmatch(r"[0-9a-f]{32}", article_id):
        return _safe_article_image(article_id, relative, database)
    root = (PROJECT_ROOT / "data" / "article-assets").resolve(strict=False)
    try:
        root.relative_to(PROJECT_ROOT.resolve(strict=False))
    except ValueError:
        return None
    candidate = PROJECT_ROOT / relative
    if candidate.is_symlink():
        return None
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
        if not candidate.is_file() or candidate.stat().st_size > 10_000_000:
            return None
    except (OSError, ValueError):
        return None
    return candidate


def markdown_to_html(markdown: str) -> str:
    parts = [part.strip() for part in re.split(r"\n\s*\n", markdown) if part.strip()]
    output: list[str] = []
    for part in parts:
        lines = part.splitlines()
        if part.startswith("#"):
            first = lines[0]
            level = min(4, max(1, len(first) - len(first.lstrip("#"))))
            output.append(f"<h{level}>{html.escape(first.lstrip('# ').strip())}</h{level}>")
        elif part.startswith("> "):
            output.append("<blockquote>" + "<br/>".join(html.escape(line[2:] if line.startswith("> ") else line) for line in lines) + "</blockquote>")
        elif all(re.match(r"^\s*(?:[-*+]\s+|\d+\.\s+)", line) for line in lines):
            tag = "ol" if re.match(r"^\s*\d+\.\s+", lines[0]) else "ul"
            lis = "".join("<li>" + html.escape(re.sub(r"^\s*(?:[-*+]\s+|\d+\.\s+)", "", line)) + "</li>" for line in lines)
            output.append(f"<{tag}>{lis}</{tag}>")
        else:
            output.append("<p>" + "<br/>".join(html.escape(line) for line in lines) + "</p>")
    return "\n".join(output)


def _run_weibo(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    executable = _weibo_cli_executable()
    if not executable:
        raise FileNotFoundError("微博开放平台官方 CLI 未安装。")
    command = _weibo_command(executable, args)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _weibo_command(executable: str, args: list[str]) -> list[str]:
    if not executable.casefold().endswith((".cmd", ".bat")):
        return [executable, *args]
    node = shutil.which("node")
    package_root = Path(executable).parent / "node_modules" / "@weibo-ai" / "weibo-cli"
    manifest_path = package_root / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    bins = manifest.get("bin") if isinstance(manifest, dict) else None
    binary_name = Path(executable).stem
    relative_entry = bins.get(binary_name) if isinstance(bins, dict) else None
    try:
        entry = (package_root / str(relative_entry)).resolve()
        entry.relative_to(package_root.resolve())
    except (OSError, ValueError):
        entry = None
    if not node or not isinstance(relative_entry, str) or entry is None or not entry.is_file():
        raise FileNotFoundError(
            "无法定位微博官方 CLI 的 Node.js 入口；请确认全局 npm 安装完整并安装 Node.js 18+。"
        )
    return [node, str(entry), *args]


def _parse_json(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text.strip())
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _platform_error(value: dict[str, Any], fallback: str) -> str:
    code = value.get("errcode") or value.get("code") or ""
    message = value.get("errmsg") or value.get("message") or value.get("error_description") or ""
    clean = _safe_error(str(message))[:250]
    return f"{fallback}（{str(code)[:40]}：{clean}）" if code and clean else f"{fallback}（{clean}）" if clean else fallback


def _safe_error(value: str) -> str:
    clean = re.sub(r"(?i)(token|secret|authorization|cookie|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=<已隐藏>", value)
    return re.sub(r"https?://[^\s]+", "<链接已隐藏>", clean).strip()[:400]


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
