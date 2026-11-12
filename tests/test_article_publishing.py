from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio
import inspect
from io import BytesIO
import subprocess

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.background import BackgroundTasks

from app.workbench import api as workbench_api
from app.workbench import article_studio
from app.workbench.article_studio import (
    create_article,
    default_document,
    finalize_article,
    latest_publication,
    list_articles,
    reserve_article_publication,
    recover_article_publications,
    update_article_publication,
    update_article_fan_notification,
)
from app.workbench.database import Database, SCHEMA_VERSION, _MIGRATIONS
from app.workbench.config import AppConfig
from app.workbench.runner import TaskRunner
from app.workbench import article_platforms


def _finalized_article(database: Database) -> dict:
    document = default_document("发布闭环测试")
    document["body_markdown"] = "这是一篇用于发布流程测试的文章。"
    document["cover_image_path"] = "data/article-assets/test/cover.png"
    article = create_article(database, document)
    finalized = finalize_article(database, article["id"])
    assert finalized is not None
    return finalized


def test_csdn_tag_lookup_uses_article_content_and_preserves_platform_limits(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "tag-lookup.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database))
    calls = []

    class Manager:
        def get_csdn_tags(self, document, keyword):
            calls.append((document["title"], document["body_markdown"], keyword))
            return {"categories": [{"name": "Python", "tags": ["python", "django"]}], "max_tags": 7, "results": ["python"], "declarations": ["无声明", "部分内容由AI辅助生成"]}

    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: Manager())
    response = workbench_api.lookup_csdn_article_tags(article["id"], workbench_api.CsdnTagLookupRequest(keyword="python"), SimpleNamespace())
    assert calls == [(article["document"]["title"], article["document"]["body_markdown"], "python")]
    assert response["max_tags"] == 7
    assert response["categories"][0]["tags"] == ["python", "django"]
    assert response["declarations"] == ["无声明", "部分内容由AI辅助生成"]


def test_csdn_tag_lookup_missing_article_does_not_open_platform(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "tag-missing.sqlite3")
    database.migrate()
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database))
    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: pytest.fail("unexpected browser"))
    with pytest.raises(HTTPException) as exc:
        workbench_api.lookup_csdn_article_tags("missing", workbench_api.CsdnTagLookupRequest(), SimpleNamespace())
    assert exc.value.status_code == 404


def test_xiaohongshu_article_is_reserved_for_manual_handoff_without_browser(
    tmp_path: Path, monkeypatch,
) -> None:
    database = Database(tmp_path / "manual-xhs.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    config = AppConfig(database)
    request = SimpleNamespace()
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=config))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [
        ("xiaohongshu", "xhs-account", "小红书账号", {"title": "标题", "note": "正文"}),
    ])
    monkeypatch.setattr(workbench_api, "xiaohongshu_article_images", lambda *_args, **_kwargs: [Path("cover.png")])
    monkeypatch.setattr(workbench_api, "_video_publish_manager", lambda _request: pytest.fail("不能启动小红书浏览器"))
    tasks = BackgroundTasks()
    response = workbench_api.publish_article_endpoint(
        article["id"], workbench_api.ArticlePublishRequest(confirmed=True), request, tasks,
    )
    assert response["results"][0]["status"] == "manual_required"
    assert not tasks.tasks


def test_xiaohongshu_manual_handoff_can_be_confirmed_for_current_account(
    tmp_path: Path, monkeypatch,
) -> None:
    database = Database(tmp_path / "manual-xhs-confirm.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(database, article["id"], "xiaohongshu", article["version"], account_id="xhs-account")
    update_article_publication(database, publication["id"], "manual_required")
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database))
    result = workbench_api.confirm_xiaohongshu_article_publication(
        article["id"], publication["id"],
        workbench_api.ArticlePublishResultConfirmRequest(confirmed=True, outcome="published"), SimpleNamespace(),
    )
    assert result["status"] == "manual_published"


def test_xiaohongshu_old_queued_execution_cannot_open_a_browser(tmp_path: Path) -> None:
    database = Database(tmp_path / "queued-xhs.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(database, article["id"], "xiaohongshu", article["version"], account_id="xhs-account")

    class Manager:
        def publish_xiaohongshu_note(self, *_args):
            pytest.fail("不能启动小红书浏览器")

    workbench_api._execute_article_publication(database, AppConfig(database), publication["id"], "xiaohongshu", "xhs-account", article["document"], Manager())
    result = latest_publication(database, article["id"], "xiaohongshu", article["version"], account_id="xhs-account")
    assert result["status"] == "manual_required"


def test_manual_adapter_capability_controls_task_dispatch(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "manual-adapter.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("weibo", None, None, {})])
    monkeypatch.setattr(workbench_api, "article_platform_capabilities", lambda _platform: {"article": {"publish": "manual"}}, raising=False)
    tasks = BackgroundTasks()
    response = workbench_api.publish_article_endpoint(article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks)
    assert response["results"][0]["status"] == "manual_required"
    assert not tasks.tasks


def test_csdn_supported_form_schedules_browser_publication_with_frozen_options(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "csdn-browser-route.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    options = {"tags": ["Python"], "csdn": {"article_type": "original", "visibility": "private"}}
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("csdn", None, None, options)])
    tasks = BackgroundTasks()
    response = workbench_api.publish_article_endpoint(article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks)
    assert response["results"][0]["status"] == "publishing"
    assert len(tasks.tasks) == 1
    assert tasks.tasks[0].args[5]["_publish_options"] == options


def test_csdn_unsupported_settings_and_legacy_requests_remain_manual(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "csdn-manual-fallback.sqlite3")
    database.migrate()
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    for options in ({}, {"tags": ["Python"], "csdn": {"category": "我的分类"}}):
        article = _finalized_article(database)
        monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("csdn", None, None, options)])
        tasks = BackgroundTasks()
        response = workbench_api.publish_article_endpoint(article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks)
        assert response["results"][0]["status"] == "manual_required"
        assert not tasks.tasks
        if options: assert "分类" in response["results"][0]["message"]


def test_toutiao_request_preserves_default_cover_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "toutiao", "name": "今日头条", "accounts": [], "connected": True,
    }])
    request = workbench_api.ArticlePublishRequest(
        confirmed=True,
        targets=[workbench_api.ArticlePublishTarget(
            platform_id="toutiao",
            options=workbench_api.ArticlePublishOptions(
                toutiao=workbench_api.ToutiaoPublishOptions(cover_mode="none"),
            ),
        )],
    )
    resolved = workbench_api._resolve_article_publish_targets(request, object(), object())
    settings = resolved[0][3]["toutiao"]
    assert settings["cover_mode"] == "none"
    assert settings["cover_image_paths"] == []
    assert settings["account_options_verified"] is False


def test_toutiao_publish_target_snapshots_connected_profile_name(monkeypatch) -> None:
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "toutiao", "name": "今日头条", "connected": True,
        "accounts": [{"id": "toutiao-local-profile", "name": "测试头条号", "configured": True}],
    }])
    request = workbench_api.ArticlePublishRequest(platforms=["toutiao"], confirmed=True)

    resolved = workbench_api._resolve_article_publish_targets(request, object(), object())

    assert resolved == [("toutiao", None, "测试头条号", {})]


def test_toutiao_missing_account_options_is_held_for_manual_publish(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "toutiao-options-unavailable.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    config = AppConfig(database)
    options = {
        "toutiao": {
            "cover_mode": "none", "cover_image_paths": [], "account_options_verified": False,
        },
    }
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=config))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [
        ("toutiao", None, None, options),
    ])
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "toutiao", "connected": True,
    }])
    monkeypatch.setattr(workbench_api, "article_platform_capabilities", lambda _platform: {"article": {"publish": "browser"}}, raising=False)
    tasks = BackgroundTasks()
    response = workbench_api.publish_article_endpoint(
        article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks,
    )
    assert response["results"][0]["status"] == "manual_required"
    assert "发表选项" in response["results"][0]["message"]
    assert not tasks.tasks


def test_csdn_selected_tags_are_saved_with_publish_options(tmp_path: Path) -> None:
    database = Database(tmp_path / "csdn-tag-options.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    options = workbench_api.ArticlePublishOptions(title="文章标题", tags=["Python", "人工智能"])
    publication, _ = reserve_article_publication(database, article["id"], "csdn", article["version"], options=options.model_dump(exclude_none=True))
    result = latest_publication(database, article["id"], "csdn", article["version"])
    assert result is not None
    assert result["id"] == publication["id"]
    assert result["options"]["tags"] == ["Python", "人工智能"]


@pytest.mark.parametrize("tags", [["x"] * 51, [""], ["a" * 101]])
def test_csdn_invalid_tags_are_rejected(tags: list[str]) -> None:
    with pytest.raises(ValidationError):
        workbench_api.ArticlePublishOptions(tags=tags)


def test_csdn_preflight_refuses_unselected_tags_before_any_browser_activity(tmp_path: Path) -> None:
    database = Database(tmp_path / "csdn-preflight.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    with pytest.raises(ValueError, match="标签"):
        article_platforms.prepare_csdn_publication(article["document"], [])


def test_csdn_preflight_uses_only_user_selected_tags() -> None:
    document = default_document("标题")
    document["body_markdown"] = "正文"
    prepared = article_platforms.prepare_csdn_publication(document, [" Python ", "人工智能"])
    assert prepared["title"] == "标题"
    assert prepared["body_markdown"] == "正文"
    assert prepared["tags"] == ["Python", "人工智能"]


def test_csdn_publish_form_defaults_to_original_and_freezes_settings(tmp_path: Path) -> None:
    database = Database(tmp_path / "csdn-form.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    options = workbench_api.ArticlePublishOptions(tags=["Python"], csdn={"visibility": "followers", "article_type": "reprint", "source_url": "https://example.org/article", "summary": "摘要", "declaration": "部分内容由AI辅助生成"})
    assert workbench_api.CsdnPublishOptions().article_type == "original"
    publication, _ = reserve_article_publication(database, article["id"], "csdn", article["version"], options=options.model_dump(exclude_none=True))
    assert publication["options"]["csdn"]["visibility"] == "followers"
    assert publication["options"]["csdn"]["article_type"] == "reprint"
    assert publication["options"]["csdn"]["declaration"] == "部分内容由AI辅助生成"


def test_csdn_publish_form_rejects_unknown_visibility() -> None:
    with pytest.raises(ValidationError):
        workbench_api.CsdnPublishOptions(visibility="unknown")


def test_foreign_platform_cover_is_rejected_before_any_target_is_queued(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "foreign-cover.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    foreign_ref = f'data/article-assets/{"b" * 32}/{"c" * 32}.png'
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("wechat", "account", "公众号", {"cover_image_path": foreign_ref})])
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        workbench_api.publish_article_endpoint(article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks)
    assert exc.value.status_code == 422
    assert not tasks.tasks
    assert not workbench_api.get_article(database, article["id"])["publications"]


def test_csdn_form_without_selected_tags_is_rejected_before_reservation(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "missing-csdn-tags.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("csdn", None, None, {"csdn": {"article_type": "original"}})])
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        workbench_api.publish_article_endpoint(article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks)
    assert exc.value.status_code == 422
    assert not tasks.tasks
    assert not workbench_api.get_article(database, article["id"])["publications"]


def test_csdn_submission_interruption_cannot_be_retried_automatically(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "csdn-interruption.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(database, article["id"], "csdn", article["version"])

    class Manager:
        def publish_csdn_article(self, _document, _options, _cover_path, before_submit):
            before_submit()
            raise RuntimeError("connection lost")

    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: Manager())
    workbench_api._execute_article_publication(database, AppConfig(database), publication["id"], "csdn", None, {**article["document"], "cover_image_path": "", "_article_id": article["id"], "_publish_options": {"tags": ["Python"]}})
    result = latest_publication(database, article["id"], "csdn", article["version"])
    assert result["status"] == "unknown"
    repeated, created = reserve_article_publication(database, article["id"], "csdn", article["version"])
    assert not created
    assert repeated["id"] == publication["id"]


def test_csdn_failure_before_submission_can_be_retried(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "csdn-failure.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(database, article["id"], "csdn", article["version"])

    class Manager:
        def publish_csdn_article(self, *_args):
            raise RuntimeError("平台未接受文章标签")

    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: Manager())
    workbench_api._execute_article_publication(database, AppConfig(database), publication["id"], "csdn", None, {**article["document"], "cover_image_path": "", "_article_id": article["id"], "_publish_options": {"tags": ["Python"]}})
    result = latest_publication(database, article["id"], "csdn", article["version"])
    assert result["status"] == "failed"
    assert "标签" in result["message"]


@pytest.mark.parametrize(("outcome", "expected"), [("published", "published"), ("not_published", "failed")])
def test_csdn_uncertain_result_can_be_confirmed_once_by_user(tmp_path: Path, monkeypatch, outcome: str, expected: str) -> None:
    database = Database(tmp_path / "csdn-confirm.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(database, article["id"], "csdn", article["version"])
    update_article_publication(database, publication["id"], "unknown")
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database))
    body = workbench_api.ArticlePublishResultConfirmRequest(confirmed=True, outcome=outcome)
    result = workbench_api.confirm_csdn_article_publication(article["id"], publication["id"], body, SimpleNamespace())
    assert result["status"] == expected
    with pytest.raises(HTTPException) as exc:
        workbench_api.confirm_csdn_article_publication(article["id"], publication["id"], body, SimpleNamespace())
    assert exc.value.status_code == 409


def test_article_publication_reservation_is_idempotent_per_account(tmp_path: Path) -> None:
    database = Database(tmp_path / "articles.sqlite3")
    database.migrate()
    article = _finalized_article(database)

    first, created_first = reserve_article_publication(
        database,
        article["id"],
        "wechat",
        article["version"],
        account_id="wechat-acct-a",
        account_name="品牌公众号",
    )
    repeated, created_repeated = reserve_article_publication(
        database,
        article["id"],
        "wechat",
        article["version"],
        account_id="wechat-acct-a",
        account_name="品牌公众号",
    )
    second_account, created_second = reserve_article_publication(
        database,
        article["id"],
        "wechat",
        article["version"],
        account_id="wechat-acct-b",
        account_name="个人公众号",
    )

    assert created_first is True
    assert created_repeated is False
    assert repeated["id"] == first["id"]
    assert created_second is True
    assert second_account["id"] != first["id"]
    assert first["status"] == "publishing"
    assert first["account_name"] == "品牌公众号"


def test_toutiao_manual_publication_requires_explicit_retry_of_same_record(tmp_path: Path) -> None:
    database = Database(tmp_path / "toutiao-manual-retry.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    manual, created = reserve_article_publication(database, article["id"], "toutiao", article["version"])
    assert created is True
    update_article_publication(database, manual["id"], "manual_required", message="之前未自动提交。")

    unchanged, created_without_confirmation = reserve_article_publication(
        database, article["id"], "toutiao", article["version"],
    )
    assert created_without_confirmation is False
    assert unchanged["id"] == manual["id"]

    retried, created_with_confirmation = reserve_article_publication(
        database,
        article["id"],
        "toutiao",
        article["version"],
        retry_manual_publication_id=manual["id"],
    )
    assert created_with_confirmation is True
    assert retried["id"] != manual["id"]
    assert retried["status"] == "publishing"


def test_toutiao_verified_retry_requeues_only_supported_manual_record(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "toutiao-route-retry.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    previous, _ = reserve_article_publication(database, article["id"], "toutiao", article["version"])
    update_article_publication(database, previous["id"], "manual_required", message="上一次未自动提交。")
    options = {
        "title": article["document"]["title"],
        "retry_publication_id": previous["id"],
        "toutiao": {
            "cover_mode": "none",
            "cover_image_paths": [],
            "account_options_verified": True,
        },
    }
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("toutiao", None, None, options)])
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{"id": "toutiao", "connected": True}])
    monkeypatch.setattr(workbench_api, "article_platform_capabilities", lambda _platform: {"article": {"publish": "browser"}}, raising=False)

    tasks = BackgroundTasks()
    response = workbench_api.publish_article_endpoint(
        article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks,
    )

    assert response["results"][0]["status"] == "publishing", response["results"][0]["message"]
    assert response["results"][0]["id"] != previous["id"]
    latest_article = workbench_api.get_article(database, article["id"])
    assert next(item for item in latest_article["publications"] if item["id"] == previous["id"])["status"] == "manual_required"
    assert len(tasks.tasks) == 1


def test_toutiao_retry_rejects_stale_or_non_manual_record(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "toutiao-stale-retry.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    previous, _ = reserve_article_publication(database, article["id"], "toutiao", article["version"])
    options = {
        "retry_publication_id": previous["id"],
        "toutiao": {"cover_mode": "none", "cover_image_paths": [], "account_options_verified": True},
    }
    monkeypatch.setattr(workbench_api, "_runner", lambda _request: SimpleNamespace(database=database, config=AppConfig(database)))
    monkeypatch.setattr(workbench_api, "_resolve_article_publish_targets", lambda *_args: [("toutiao", None, None, options)])
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{"id": "toutiao", "connected": True}])
    tasks = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        workbench_api.publish_article_endpoint(
            article["id"], workbench_api.ArticlePublishRequest(confirmed=True), SimpleNamespace(), tasks,
        )
    assert exc.value.status_code == 409
    assert not tasks.tasks


def test_weibo_status_reports_cli_missing_without_claiming_account_connection(monkeypatch) -> None:
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: None)

    status = article_platforms._weibo_status()

    assert status["cli_installed"] is False
    assert status["connected"] is False
    assert status["configured"] is False
    assert status["publish_ready"] is False
    assert status["connection_checked"] is False
    assert status["readiness_checked"] is False
    assert "Node.js 18" in status["message"]


def test_weibo_status_separates_login_from_developer_and_service_readiness(monkeypatch) -> None:
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: "weibo")
    calls = []

    def run_weibo(args, *, timeout):
        calls.append((args, timeout))
        if args[:2] == ["auth", "whoami"]:
            return article_platforms.subprocess.CompletedProcess(
                args, 0, '{"user":{"username":"阿青"}}', "",
            )
        return article_platforms.subprocess.CompletedProcess(
            args, 0,
            '{"ready":false,"steps":{"login":true,"developer_verification":true,"service":false},"urls":{"plan":"https://open.weibo.com/plan"},"user":{"username":"阿青"},"service_status":{"kind":"none"}}',
            "",
        )

    monkeypatch.setattr(article_platforms, "_run_weibo", run_weibo)
    monkeypatch.setattr(article_platforms, "_weibo_login_active", lambda: False)

    status = article_platforms._weibo_status()

    assert status["cli_installed"] is True
    assert status["connected"] is True
    assert status["connection_checked"] is True
    assert status["readiness_checked"] is True
    assert status["configured"] is True
    assert status["publish_ready"] is False
    assert status["name"] == "阿青"
    assert status["developer_verified"] is True
    assert status["service_ready"] is False
    assert status["service_url"] == "https://open.weibo.com/plan"
    assert [call[0] for call in calls] == [
        ["auth", "whoami", "--output", "json"],
        ["doctor", "--output", "json"],
    ]


def test_weibo_status_does_not_claim_connection_when_whoami_fails(monkeypatch) -> None:
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: "weibo")
    calls = []

    def run_weibo(args, *, timeout):
        calls.append(args)
        return article_platforms.subprocess.CompletedProcess(args, 1, "", "not logged in")

    monkeypatch.setattr(article_platforms, "_run_weibo", run_weibo)
    monkeypatch.setattr(article_platforms, "_weibo_login_active", lambda: False)

    status = article_platforms._weibo_status()

    assert status["cli_installed"] is True
    assert status["connected"] is False
    assert status["publish_ready"] is False
    assert status["connection_checked"] is False
    assert len(calls) == 1
    assert "未能确认账号连接状态" in status["message"]


def test_weibo_status_does_not_misreport_doctor_failure_as_missing_setup(monkeypatch) -> None:
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: "weibo")
    replies = iter([
        article_platforms.subprocess.CompletedProcess([], 0, '{"user":{"username":"阿青"}}', ""),
        article_platforms.subprocess.CompletedProcess([], 1, "", "temporary network error"),
    ])
    monkeypatch.setattr(article_platforms, "_run_weibo", lambda _args, *, timeout: next(replies))
    monkeypatch.setattr(article_platforms, "_weibo_login_active", lambda: False)

    status = article_platforms._weibo_status()

    assert status["connected"] is True
    assert status["connection_checked"] is True
    assert status["readiness_checked"] is False
    assert status["publish_ready"] is False
    assert "暂未确认微博发布准备状态" in status["message"]
    assert "完成开发者认证" not in status["message"]


def test_weibo_status_requires_doctor_readiness_before_enabling_publication(monkeypatch) -> None:
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: "weibo")
    replies = iter([
        article_platforms.subprocess.CompletedProcess(
            [], 0, '{"user":{"username":"阿青"}}', "",
        ),
        article_platforms.subprocess.CompletedProcess(
            [], 0,
            '{"ready":true,"steps":{"login":true,"developer_verification":true,"service":true},"user":{"username":"阿青"}}',
            "",
        ),
    ])
    monkeypatch.setattr(article_platforms, "_run_weibo", lambda _args, *, timeout: next(replies))
    monkeypatch.setattr(article_platforms, "_weibo_login_active", lambda: False)

    status = article_platforms._weibo_status()

    assert status["connected"] is True
    assert status["connection_checked"] is True
    assert status["readiness_checked"] is True
    assert status["publish_ready"] is True
    assert status["configured"] is True
    assert status["developer_verified"] is True
    assert status["service_ready"] is True


def test_weibo_login_opens_official_browser_flow_in_new_console(tmp_path: Path, monkeypatch) -> None:
    import os

    article_platforms._WEIBO_LOGIN_PROCESS = None
    prefix = tmp_path / "npm"
    executable = prefix / "weibo.cmd"
    package = prefix / "node_modules" / "@weibo-ai" / "weibo-cli"
    entry = package / "dist" / "index.js"
    package.mkdir(parents=True)
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    (package / "package.json").write_text(
        '{"name":"@weibo-ai/weibo-cli","bin":{"weibo":"dist/index.js"}}',
        encoding="utf-8",
    )
    executable.write_text("@echo off", encoding="utf-8")
    node = tmp_path / "node.exe"
    monkeypatch.setattr(
        article_platforms.shutil,
        "which",
        lambda name: str(node) if name == "node" else str(executable) if name == "weibo-cli" else None,
    )
    launched = {}

    class Process:
        def poll(self):
            return None

    def popen(command, **kwargs):
        launched["command"] = command
        launched["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(article_platforms.subprocess, "Popen", popen)

    result = article_platforms.start_weibo_login()

    assert result["started"] is True
    assert launched["command"] == [str(node), str(entry), "auth", "login", "--browser"]
    assert launched["kwargs"]["creationflags"] & getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
    assert launched["kwargs"]["stdin"] is None
    assert article_platforms._weibo_login_active() is True
    assert os.name == "nt"


def test_weibo_windows_cli_shim_uses_node_entry_without_cmd(tmp_path: Path, monkeypatch) -> None:
    prefix = tmp_path / "npm-prefix"
    shim = prefix / "weibo.cmd"
    package = prefix / "node_modules" / "@weibo-ai" / "weibo-cli"
    entry = package / "dist" / "index.js"
    package.mkdir(parents=True)
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    (package / "package.json").write_text(
        '{"name":"@weibo-ai/weibo-cli","bin":{"weibo":"dist/index.js"}}',
        encoding="utf-8",
    )
    shim.write_text("@echo off", encoding="utf-8")
    node = tmp_path / "node.exe"
    monkeypatch.setattr(article_platforms.shutil, "which", lambda name: str(node) if name == "node" else None)

    command = article_platforms._weibo_command(str(shim), ["article", "publish", "--content", "正文 & echo injected"])

    assert command == [str(node), str(entry), "article", "publish", "--content", "正文 & echo injected"]
    assert all("cmd.exe" not in part.casefold() for part in command)


def test_weibo_login_requires_cli_before_starting_authentication(monkeypatch) -> None:
    article_platforms._WEIBO_LOGIN_PROCESS = None
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: None)

    with pytest.raises(FileNotFoundError, match="安装微博官方 CLI"):
        article_platforms.start_weibo_login()
    monkeypatch.setattr(article_platforms.shutil, "which", lambda _name: "weibo")
    monkeypatch.setattr(article_platforms.subprocess, "Popen", lambda *_args, **_kwargs: SimpleNamespace(poll=lambda: None))

    article_platforms.start_weibo_login()
    with pytest.raises(article_platforms.PlatformConnectionError, match="正在进行"):
        article_platforms.start_weibo_login()

    article_platforms._WEIBO_LOGIN_PROCESS = None


def test_weibo_publish_marks_response_without_remote_receipt_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        article_platforms, "_run_weibo",
        lambda *_args, **_kwargs: article_platforms.subprocess.CompletedProcess([], 0, '{"result":{"success":true}}', ""),
    )

    result = article_platforms._publish_weibo({"title": "标题", "body_markdown": "正文"})

    assert result["status"] == "unknown"
    assert result["remote_id"] is None


def test_weibo_publish_reads_remote_receipt_from_cli_result_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        article_platforms, "_run_weibo",
        lambda *_args, **_kwargs: article_platforms.subprocess.CompletedProcess(
            [], 0, '{"result":{"article_id":"wb-123","url":"https://weibo.com/ttarticle/p/show?id=wb-123"},"outcome":"success"}', "",
        ),
    )

    result = article_platforms._publish_weibo({"title": "标题", "body_markdown": "正文"})

    assert result["status"] == "published"
    assert result["remote_id"] == "wb-123"
    assert result["remote_url"] == "https://weibo.com/ttarticle/p/show?id=wb-123"


def test_weibo_publish_keeps_non_success_cli_outcome_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        article_platforms, "_run_weibo",
        lambda *_args, **_kwargs: article_platforms.subprocess.CompletedProcess(
            [], 0, '{"result":{"article_id":"wb-123"},"outcome":"uncertain"}', "",
        ),
    )

    result = article_platforms._publish_weibo({"title": "标题", "body_markdown": "正文"})

    assert result["status"] == "unknown"
    assert result["remote_id"] == "wb-123"


def test_weibo_publish_keeps_nonzero_cli_exit_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        article_platforms, "_run_weibo",
        lambda *_args, **_kwargs: article_platforms.subprocess.CompletedProcess([], 1, "", "network error"),
    )

    result = article_platforms._publish_weibo({"title": "标题", "body_markdown": "正文"})

    assert result["status"] == "unknown"
    assert "核对" in result["message"]


def test_weibo_publish_keeps_timeout_unknown(monkeypatch) -> None:
    def timeout(*_args, **_kwargs):
        raise article_platforms.subprocess.TimeoutExpired("weibo", 90)

    monkeypatch.setattr(article_platforms, "_run_weibo", timeout)

    result = article_platforms._publish_weibo({"title": "标题", "body_markdown": "正文"})

    assert result["status"] == "unknown"
    assert "核对" in result["message"]


def test_weibo_nonzero_cli_exit_is_saved_as_unknown_publication(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "weibo-ambiguous.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, created = reserve_article_publication(database, article["id"], "weibo", article["version"])
    assert created is True
    monkeypatch.setattr(
        article_platforms, "_run_weibo",
        lambda *_args, **_kwargs: article_platforms.subprocess.CompletedProcess([], 1, "", "network error"),
    )

    workbench_api._execute_article_publication(
        database,
        AppConfig(database),
        publication["id"],
        "weibo",
        None,
        {"_article_id": article["id"], "title": "标题", "body_markdown": "正文"},
        None,
    )

    latest = latest_publication(database, article["id"], "weibo", article["version"])
    assert latest is not None
    assert latest["status"] == "unknown"


def test_weibo_article_target_requires_cli_publish_readiness(monkeypatch) -> None:
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "weibo", "name": "微博", "mode": "official", "configured": True,
        "connected": True, "connection_checked": True, "readiness_checked": True,
        "cli_installed": True, "publish_ready": False,
        "message": "请先开通微博开放平台服务。", "accounts": [],
    }])
    request = workbench_api.ArticlePublishRequest(platforms=["weibo"], confirmed=True)

    with pytest.raises(HTTPException) as error:
        workbench_api._resolve_article_publish_targets(request, object(), object())

    assert error.value.status_code == 409
    assert "开通微博开放平台服务" in error.value.detail


def test_weibo_article_target_preserves_unknown_connection_state(monkeypatch) -> None:
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "weibo", "name": "微博", "mode": "official", "configured": True,
        "connected": False, "connection_checked": False, "cli_installed": True,
        "publish_ready": False, "message": "微博 CLI 暂时未能确认连接状态。", "accounts": [],
    }])
    request = workbench_api.ArticlePublishRequest(platforms=["weibo"], confirmed=True)

    with pytest.raises(HTTPException) as error:
        workbench_api._resolve_article_publish_targets(request, object(), object())

    assert error.value.status_code == 409
    assert "无法确认微博 CLI 当前账号连接状态" in error.value.detail


def test_weibo_article_target_resolves_after_cli_is_ready(monkeypatch) -> None:
    monkeypatch.setattr(workbench_api, "_article_platform_statuses", lambda *_args: [{
        "id": "weibo", "name": "微博", "mode": "official", "configured": True,
        "connected": True, "connection_checked": True, "readiness_checked": True,
        "cli_installed": True, "publish_ready": True,
        "message": "已就绪。", "accounts": [],
    }])
    request = workbench_api.ArticlePublishRequest(platforms=["weibo"], confirmed=True)

    assert workbench_api._resolve_article_publish_targets(request, object(), object()) == [
        ("weibo", None, None, {}),
    ]


def test_weibo_login_route_starts_official_login_with_same_origin(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(workbench_api._router)
    monkeypatch.setattr(workbench_api, "start_weibo_login", lambda: {
        "started": True, "message": "已打开微博官方登录窗口。",
    })

    with TestClient(app, base_url="http://testserver") as client:
        response = client.post(
            "/api/v1/article-platforms/weibo/login",
            headers={"Origin": "http://testserver"},
        )

    assert response.status_code == 200
    assert response.json() == {"started": True, "message": "已打开微博官方登录窗口。"}


def test_csdn_editor_and_session_routes_use_the_app_managed_profile_with_same_origin(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    calls = []
    manager = SimpleNamespace(
        open_workbench=lambda platform: calls.append(("workbench", platform)) or {
            "id": "csdn", "status": "login_window_open", "authenticated": False,
            "login_window_open": True, "message": "登录窗口已打开。",
        },
        clear_account=lambda platform: calls.append(("clear", platform)) or {
            "id": "csdn", "status": "not_connected", "authenticated": False,
            "login_window_open": False, "message": "尚未检测到平台登录会话。",
        },
    )
    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: manager)
    app = FastAPI()
    app.include_router(workbench_api._router)

    with TestClient(app, base_url="http://testserver") as client:
        opened = client.post(
            "/api/v1/article-platforms/csdn/editor",
            headers={"Origin": "http://testserver"},
        )
        cleared = client.delete(
            "/api/v1/article-platforms/csdn/session",
            headers={"Origin": "http://testserver"},
        )

    assert opened.status_code == 200
    assert opened.json()["status"] == "login_window_open"
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "not_connected"
    assert calls == [("workbench", "csdn"), ("clear", "csdn")]


def test_toutiao_editor_and_session_routes_use_the_app_managed_profile_with_same_origin(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    calls = []
    manager = SimpleNamespace(
        open_workbench=lambda platform: calls.append(("workbench", platform)) or {
            "id": "toutiao", "status": "login_window_open", "authenticated": False,
            "login_window_open": True, "message": "头条号创作页已在工作台窗口打开。",
        },
        clear_account=lambda platform: calls.append(("clear", platform)) or {
            "id": "toutiao", "status": "not_connected", "authenticated": False,
            "login_window_open": False, "message": "尚未检测到平台登录会话。",
        },
    )
    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: manager)
    app = FastAPI()
    app.include_router(workbench_api._router)

    with TestClient(app, base_url="http://testserver") as client:
        opened = client.post(
            "/api/v1/article-platforms/toutiao/editor",
            headers={"Origin": "http://testserver"},
        )
        cleared = client.delete(
            "/api/v1/article-platforms/toutiao/session",
            headers={"Origin": "http://testserver"},
        )

    assert opened.status_code == 200
    assert opened.json()["status"] == "login_window_open"
    assert cleared.status_code == 200
    assert cleared.json()["status"] == "not_connected"
    assert calls == [("workbench", "toutiao"), ("clear", "toutiao")]


def test_csdn_article_platform_status_reflects_local_browser_session(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "csdn-accounts.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [{
        "id": "csdn", "name": "CSDN", "mode": "manual", "configured": False,
        "connected": False, "message": "当前提供手动发布。", "accounts": [],
        "secret_available": True, "public_config": {},
        "manual_url": "https://editor.csdn.net/md/", "authorization_url": None,
    }])
    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: SimpleNamespace(
        account_status=lambda platform: {
            "id": platform, "status": "connected", "authenticated": True,
            "login_window_open": False, "message": "检测到平台登录会话。",
            "profile_name": "测试作者", "profile_username": "test_writer_42",
            "profile_avatar_url": "https://i-avatar.csdnimg.cn/avatar_test_writer_42.jpg!1",
        },
    ))

    status = workbench_api._article_platform_statuses(config, database)[0]

    assert status["configured"] is True
    assert status["connected"] is True
    assert status["mode"] == "manual"
    assert status["manual_url"] == "https://editor.csdn.net/md/"
    assert status["accounts"] == [{
        "id": "csdn-local-profile", "name": "测试作者", "public_value": "test_writer_42",
        "credential_verified_at": None, "credential_verified": True, "configured": True,
        "secret_available": True, "status": "connected",
        "message": "检测到平台登录会话。", "auth_type": "browser",
        "avatar_url": "https://i-avatar.csdnimg.cn/avatar_test_writer_42.jpg!1",
    }]


def test_toutiao_article_platform_status_reflects_local_browser_session_and_stays_manual(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "toutiao-accounts.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [{
        "id": "toutiao", "name": "今日头条", "mode": "manual", "configured": False,
        "connected": False, "message": "当前提供手动发布。", "accounts": [],
        "secret_available": True, "public_config": {},
        "manual_url": "https://mp.toutiao.com/profile_v4/graphic/publish", "authorization_url": None,
    }])
    monkeypatch.setattr(workbench_api, "get_platform_account_manager", lambda: SimpleNamespace(
        account_status=lambda platform: {
            "id": platform, "status": "connected", "authenticated": True,
            "login_window_open": False, "message": "头条号登录状态有效。",
            "profile_name": "测试头条号",
            "profile_avatar_url": "https://p3.toutiaoimg.com/avatar.png",
        },
    ))

    status = workbench_api._article_platform_statuses(config, database)[0]

    assert status["configured"] is True
    assert status["connected"] is True
    assert status["mode"] == "manual"
    assert status["manual_url"] == "https://mp.toutiao.com/profile_v4/graphic/publish"
    assert status["accounts"] == [{
        "id": "toutiao-local-profile", "name": "测试头条号", "public_value": "",
        "avatar_url": "https://p3.toutiaoimg.com/avatar.png",
        "credential_verified_at": None, "credential_verified": True, "configured": True,
        "secret_available": True, "status": "connected",
        "message": "头条号登录状态有效。", "auth_type": "browser",
    }]


def test_failed_publication_can_be_retried_without_overwriting_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "articles.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    failed, created = reserve_article_publication(
        database, article["id"], "zhihu", article["version"], account_id="zhihu-a", account_name="知乎主号"
    )
    assert created is True

    update_article_publication(database, failed["id"], "failed", message="凭据无效")
    retried, created_retry = reserve_article_publication(
        database, article["id"], "zhihu", article["version"], account_id="zhihu-a", account_name="知乎主号"
    )

    assert created_retry is True
    assert retried["id"] != failed["id"]
    assert retried["status"] == "publishing"
    latest_failed = update_article_publication(database, failed["id"], "failed", message="凭据无效")
    assert latest_failed["id"] == failed["id"]


def test_ambiguous_publication_cannot_be_automatically_retried(tmp_path: Path) -> None:
    database = Database(tmp_path / "articles.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    unknown, created = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号"
    )
    assert created is True
    update_article_publication(database, unknown["id"], "unknown", message="发布结果待核实")

    existing, created_again = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号"
    )

    assert created_again is False
    assert existing["id"] == unknown["id"]
    assert existing["status"] == "unknown"


def test_zhihu_article_account_status_uses_shared_sau_browser_session(tmp_path: Path, monkeypatch) -> None:
    from app.workbench.video_publishing import add_video_publish_account, update_video_publish_account_status

    database = Database(tmp_path / "zhihu-accounts.sqlite3")
    database.migrate()
    account = add_video_publish_account(database, "zhihu", "writer", "知乎主号")
    update_video_publish_account_status(database, account["id"], "connected", "登录状态有效。")
    config = AppConfig(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [{
        "id": "zhihu", "name": "知乎", "mode": "official", "configured": True,
        "connected": True, "message": "OpenAPI 凭据已验证。", "accounts": [{
            "id": "legacy", "name": "知乎 OpenAPI", "public_value": "saved-key",
            "configured": True, "credential_verified": True,
            "credential_verified_at": "2026-11-03T00:00:00Z", "secret_available": True,
        }], "secret_available": True,
        "public_config": {}, "manual_url": None, "authorization_url": None,
    }])

    status = next(item for item in workbench_api._article_platform_statuses(config, database) if item["id"] == "zhihu")

    assert status["configured"] is True
    assert status["connected"] is True
    assert [item["id"] for item in status["accounts"]] == ["legacy", "browser:" + account["id"]]
    assert status["accounts"][0]["auth_type"] == "openapi"
    linked = status["accounts"][1]
    assert linked == {
        "id": "browser:" + account["id"], "name": "知乎主号", "public_value": "writer",
        "credential_verified_at": linked["credential_verified_at"], "credential_verified": True,
        "configured": True, "secret_available": True, "status": "connected",
        "message": "登录状态有效。", "auth_type": "browser",
    }
    assert linked["credential_verified_at"]


def test_baijiahao_article_status_exposes_shared_login_account(tmp_path: Path, monkeypatch) -> None:
    from app.workbench.video_publishing import add_video_publish_account, update_video_publish_account_status

    database = Database(tmp_path / "baijiahao-accounts.sqlite3")
    database.migrate()
    account = add_video_publish_account(database, "baijiahao", "writer", "百家号主号")
    update_video_publish_account_status(database, account["id"], "connected", "登录状态有效。")
    config = AppConfig(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [{
        "id": "baijiahao", "name": "百家号", "mode": "manual", "configured": False,
        "connected": False, "message": "当前手动发布。", "accounts": [], "secret_available": True,
        "public_config": {}, "manual_url": "https://baijiahao.baidu.com/builder/rc/edit",
        "authorization_url": None,
    }])

    status = next(item for item in workbench_api._article_platform_statuses(config, database) if item["id"] == "baijiahao")

    assert status["configured"] is True
    assert status["connected"] is True
    assert status["mode"] == "manual"
    assert status["manual_url"] == "https://baijiahao.baidu.com/builder/rc/edit"
    assert status["accounts"] == [{
        "id": account["id"], "name": "百家号主号", "public_value": "writer",
        "credential_verified_at": status["accounts"][0]["credential_verified_at"],
        "credential_verified": True, "configured": True, "secret_available": True,
        "status": "connected", "message": "登录状态有效。",
    }]
    assert status["accounts"][0]["credential_verified_at"]


def test_prepare_zhihu_article_file_copies_only_article_images_and_rewrites_paths(tmp_path: Path, monkeypatch) -> None:
    article_id = "a" * 32
    image_name = "b" * 32 + ".png"
    source_root = tmp_path / "data" / "article-assets" / article_id
    source_root.mkdir(parents=True)
    source_image = source_root / image_name
    source_image.write_bytes(b"image-bytes")
    monkeypatch.setattr(article_platforms, "PROJECT_ROOT", tmp_path)
    relative = f"data/article-assets/{article_id}/{image_name}"
    document = {
        "body_markdown": f"正文\n\n![说明]({relative})",
        "blocks": [{"type": "paragraph", "text": "说明", "image_path": relative}],
    }

    with article_platforms.prepare_zhihu_article_file(article_id, document) as content_file:
        copied_image = content_file.parent / "images" / image_name
        assert content_file.is_file()
        assert copied_image.read_bytes() == b"image-bytes"
        assert f"![说明](images/{image_name})" in content_file.read_text(encoding="utf-8")
    assert not content_file.parent.exists()


@pytest.mark.parametrize(
    ("platform_status", "expected_status"),
    [("published", "published"), ("unknown", "unknown")],
)
def test_zhihu_sau_article_execution_keeps_only_verified_receipts(
    tmp_path: Path, monkeypatch, platform_status: str, expected_status: str,
) -> None:
    from app.workbench.video_publishing import SauResult

    database = Database(tmp_path / "zhihu-publish.sqlite3")
    database.migrate()
    initial_document = default_document("知乎发布测试")
    initial_document["body_markdown"] = "正文"
    article = create_article(database, initial_document)
    article_id = article["id"]
    image_name = "d" * 32 + ".png"
    source_root = tmp_path / "data" / "article-assets" / article_id
    source_root.mkdir(parents=True)
    (source_root / image_name).write_bytes(b"image-bytes")
    monkeypatch.setattr(article_platforms, "PROJECT_ROOT", tmp_path)
    relative = f"data/article-assets/{article_id}/{image_name}"
    document = default_document("知乎发布测试")
    document["body_markdown"] = f"正文\n\n![配图]({relative})"
    document["blocks"] = [{"type": "paragraph", "text": "配图", "image_path": relative}]
    from app.workbench.article_studio import save_article
    article = save_article(database, article["id"], article["version"], document)
    finalized = finalize_article(database, article["id"])
    assert finalized is not None
    publication, created = reserve_article_publication(
        database, article["id"], "zhihu", finalized["version"],
        account_id="browser:" + "e" * 32, account_name="知乎主号",
    )
    assert created is True

    class Manager:
        def publish_zhihu_article(self, account_id, publication_id, title, content_file):
            assert account_id == "e" * 32
            assert publication_id == publication["id"]
            assert title == "知乎发布测试"
            assert "![配图](images/" in content_file.read_text(encoding="utf-8")
            if platform_status == "published":
                return SauResult(0, "", platform_status="published", remote_id="123456789", remote_url="https://zhuanlan.zhihu.com/p/123456789")
            return SauResult(0, "", platform_status="unknown")

    workbench_api._execute_article_publication(
        database, AppConfig(database), publication["id"], "zhihu",
        "browser:" + "e" * 32,
        {**document, "_article_id": article["id"]},
        Manager(),
    )

    latest = latest_publication(
        database, article["id"], "zhihu", finalized["version"],
        account_id="browser:" + "e" * 32,
    )
    assert latest is not None
    assert latest["status"] == expected_status, latest.get("message")
    if expected_status == "published":
        assert latest["remote_url"] == "https://zhuanlan.zhihu.com/p/123456789"


def test_article_history_aggregates_mixed_account_results_as_partial(tmp_path: Path) -> None:
    database = Database(tmp_path / "article-history.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    published, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号甲")
    failed, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-b", account_name="公众号乙")
    update_article_publication(database, published["id"], "published", remote_id="remote-a")
    update_article_publication(database, failed["id"], "failed", message="发布权限不足")

    item = next(row for row in list_articles(database)["items"] if row["id"] == article["id"])

    assert item["publication_statuses"]["wechat"] == "partial"
    assert "wechat" in item["published_platforms"]


def test_article_history_uses_latest_attempt_for_retried_account(tmp_path: Path) -> None:
    database = Database(tmp_path / "article-retried-history.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    first, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号甲")
    update_article_publication(database, first["id"], "failed", message="第一次发送失败")
    retry, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号甲")
    update_article_publication(database, retry["id"], "published", remote_id="remote-final")

    item = next(row for row in list_articles(database)["items"] if row["id"] == article["id"])

    assert item["publication_statuses"]["wechat"] == "published"


def test_interrupted_publish_tasks_are_recovered_without_resending(tmp_path: Path) -> None:
    database = Database(tmp_path / "publish-recovery.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    zhihu, _ = reserve_article_publication(database, article["id"], "zhihu", article["version"], account_id="zhihu-a", account_name="知乎号")
    wechat_pending_id, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号甲")
    wechat_pending, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-b", account_name="公众号乙")
    update_article_publication(database, wechat_pending_id["id"], "publishing", remote_id="wechat-publish-id", message="微信已受理")

    recovered = recover_article_publications(database)
    results = {item["id"]: item for item in recovered}

    assert results[zhihu["id"]]["status"] == "unknown"
    assert results[wechat_pending["id"]]["status"] == "unknown"
    pollable = latest_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a")
    assert pollable is not None
    assert pollable["id"] == wechat_pending_id["id"]
    assert pollable["status"] == "publishing"
    assert "不要自动重发" in results[zhihu["id"]]["message"]


def test_app_startup_recovers_interrupted_publications(monkeypatch) -> None:
    app = FastAPI()
    calls: list[str] = []
    runner = SimpleNamespace(
        database=object(),
        start=lambda: calls.append("start"),
        stop=lambda: calls.append("stop"),
    )
    monkeypatch.setattr(
        workbench_api,
        "recover_article_publications",
        lambda database: calls.append("recover") or [],
    )
    monkeypatch.setattr(
        workbench_api,
        "get_platform_account_manager",
        lambda: SimpleNamespace(shutdown=lambda: calls.append("shutdown")),
    )
    monkeypatch.setattr(workbench_api, "VideoPublishManager", lambda _database: SimpleNamespace(start=lambda: None, stop=lambda: None))
    workbench_api.mount_workbench_api(app, runner)  # type: ignore[arg-type]

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            assert calls == ["start", "recover"]

    asyncio.run(run_lifespan())

    assert calls == ["start", "recover", "stop", "shutdown"]


def test_schema_migration_adds_account_metadata_and_unknown_state(tmp_path: Path) -> None:
    database = Database(tmp_path / "articles.sqlite3")
    database.migrate()

    with database.transaction() as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(article_publications)")}
        version = int(connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])

    assert {"account_id", "account_name"}.issubset(columns)
    assert version == SCHEMA_VERSION


def test_schema_migration_preserves_v15_article_publication_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy-publications.sqlite3")
    with database.transaction(write=True) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_version(version, applied_at) VALUES (15, 'before-migration')")
        for version, statements in _MIGRATIONS.items():
            if version <= 15:
                for statement in statements:
                    connection.execute(statement)
        connection.execute("INSERT INTO articles(id, title, document_json, created_at, updated_at) VALUES ('legacy-article', 'legacy', '{}', 't1', 't1')")
        connection.execute(
            """INSERT INTO article_publications
               (id, article_id, platform_id, status, remote_id, remote_url, message, created_at, updated_at, article_version)
               VALUES ('legacy-publication', 'legacy-article', 'wechat', 'published', 'remote-1', 'https://example.test/a', '旧发布记录', 't1', 't1', 3)"""
        )

    database.migrate()

    with database.transaction() as connection:
        row = connection.execute("SELECT * FROM article_publications WHERE id = 'legacy-publication'").fetchone()
        version = int(connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])
    assert row is not None
    assert row["status"] == "published"
    assert row["remote_id"] == "remote-1"
    assert row["article_version"] == 3
    assert row["account_id"] is None
    assert row["options_json"] == "{}"
    assert row["fan_notification_status"] == "not_requested"
    assert row["fan_notification_remote_id"] is None
    assert version == SCHEMA_VERSION

def test_multiple_platform_accounts_keep_secrets_isolated(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "accounts.sqlite3")
    database.migrate()
    config = AppConfig(database)
    secrets: dict[tuple[str, str | None], str] = {}
    monkeypatch.setattr(article_platforms, "set_article_platform_secret", lambda value, platform, field, account_id=None: secrets.__setitem__((platform, account_id), value))
    monkeypatch.setattr(article_platforms, "get_article_platform_secret", lambda platform, field, account_id=None: secrets.get((platform, account_id)))
    monkeypatch.setattr(article_platforms, "delete_article_platform_secret", lambda platform, field, account_id=None: secrets.pop((platform, account_id), None))

    first = article_platforms.add_platform_account(
        config, "wechat", public_value="wx_app_id_a", secret_value="secret-a", account_name="品牌号"
    )
    second = article_platforms.add_platform_account(
        config, "wechat", public_value="wx_app_id_b", secret_value="secret-b", account_name="个人号"
    )

    accounts = second["accounts"]
    account_a, account_b = accounts
    assert account_a["id"] != account_b["id"]
    assert secrets[("wechat", account_a["id"])] == "secret-a"
    assert secrets[("wechat", account_b["id"])] == "secret-b"
    assert "secret-a" not in repr(article_platforms.platform_statuses(config))
    assert "secret-b" not in repr(article_platforms.platform_statuses(config))

    updated = article_platforms.update_platform_account(
        config, "wechat", account_b["id"], account_name="个人工作号", public_value="wx_app_id_b", secret_value=None
    )
    assert updated["accounts"][1]["name"] == "个人工作号"
    assert secrets[("wechat", account_b["id"])] == "secret-b"

    article_platforms.remove_platform_account(config, "wechat", account_a["id"])
    remaining = next(item for item in article_platforms.platform_statuses(config) if item["id"] == "wechat")["accounts"]
    assert [item["id"] for item in remaining] == [account_b["id"]]
    assert ("wechat", account_a["id"]) not in secrets


def test_legacy_single_platform_credentials_remain_visible(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "legacy-accounts.sqlite3")
    database.migrate()
    config = AppConfig(database)
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, 'now')",
            ("article_platform_accounts", '{"zhihu":{"app_key":"zhihu-person"}}'),
        )
    monkeypatch.setattr(article_platforms, "get_article_platform_secret", lambda platform, field, account_id=None: "legacy-secret" if platform == "zhihu" and account_id is None else None)

    status = next(item for item in article_platforms.platform_statuses(config) if item["id"] == "zhihu")

    assert status["configured"] is True
    assert status["accounts"][0]["id"] == "legacy"
    assert status["accounts"][0]["public_value"] == "zhihu-person"


def test_wechat_publish_submits_draft_and_keeps_async_publish_id(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-publish.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda _config, _platform, _account_id: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_safe_article_cover", lambda _document: Path("cover.jpg"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_wechat_upload_cover", lambda *_args: "thumb-id")
    monkeypatch.setattr(article_platforms, "_wechat_prepare_body_images", lambda _document, _token, body, *_args: (body, []))
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(_app_id: str, _app_secret: str, url: str, *, method: str = "GET", body: dict | None = None) -> dict:
        calls.append((url, method, body))
        if "draft/add" in url:
            return {"media_id": "draft-id"}
        if "freepublish/submit" in url:
            return {"publish_id": "publish-id"}
        raise AssertionError(url)

    monkeypatch.setattr(article_platforms, "_wechat_json_request", fake_request)
    result = article_platforms.publish_article(
        config, "wechat", {"title": "标题", "summary": "摘要", "body_markdown": "正文"}, account_id="wechat-a"
    )

    assert result["status"] == "publishing"
    assert result["remote_id"] == "publish-id"
    assert [call[1] for call in calls] == ["POST", "POST"]
    assert calls[-1][2] == {"media_id": "draft-id"}


def test_wechat_publish_status_refresh_maps_pending_success_and_failure(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-status.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda _config, _platform, _account_id: ("wx_app_id", "secret"))
    responses = iter([
        {"publish_status": 1},
        {"publish_status": 0, "article_id": "article-id", "article_detail": {"item": [{"article_url": "https://mp.weixin.qq.com/s/article"}]}},
        {"publish_status": 4},
    ])
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_wechat_json_request", lambda *_args, **_kwargs: next(responses))

    pending = article_platforms.refresh_wechat_publication(config, "wechat-a", "publish-a")
    success = article_platforms.refresh_wechat_publication(config, "wechat-a", "publish-a")
    rejected = article_platforms.refresh_wechat_publication(config, "wechat-a", "publish-a")

    assert pending["status"] == "publishing"
    assert success["status"] == "published"
    assert success["remote_url"] == "https://mp.weixin.qq.com/s/article"
    assert rejected["status"] == "failed"


def test_wechat_ambiguous_submit_is_unknown_and_not_reported_published(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-ambiguous.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda _config, _platform, _account_id: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_safe_article_cover", lambda _document: Path("cover.jpg"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_wechat_upload_cover", lambda *_args: "thumb-id")
    monkeypatch.setattr(article_platforms, "_wechat_prepare_body_images", lambda _document, _token, body, *_args: (body, []))

    def fake_request(_app_id: str, _app_secret: str, url: str, **_kwargs) -> dict:
        if "freepublish/submit" in url:
            raise RuntimeError("网络断开")
        return {"media_id": "draft-id"}

    monkeypatch.setattr(article_platforms, "_wechat_json_request", fake_request)
    result = article_platforms.publish_article(config, "wechat", {"title": "标题", "body_markdown": "正文"}, account_id="wechat-a")

    assert result["status"] == "unknown"
    assert result["remote_id"] is None
    assert "草稿编号 draft-id" in result["message"]
    assert "不要直接重发" in result["message"]


def test_wechat_api_permission_rejection_is_reported_as_actionable_failure(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-permission.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda _config, _platform, _account_id: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_safe_article_cover", lambda _document: Path("cover.jpg"))
    monkeypatch.setattr(article_platforms, "_wechat_upload_cover", lambda *_args: "thumb-id")
    monkeypatch.setattr(article_platforms, "_wechat_prepare_body_images", lambda _document, _token, body, *_args: (body, []))

    def fake_urlopen(request, timeout):
        if "draft/add" in request.full_url:
            return BytesIO(b'{"media_id":"draft-id"}')
        if "freepublish/submit" in request.full_url:
            return BytesIO(b'{"errcode":48001,"errmsg":"api unauthorized rid: test-rid"}')
        raise AssertionError(request.full_url)

    monkeypatch.setattr(article_platforms.urllib.request, "urlopen", fake_urlopen)

    result = article_platforms.publish_article(
        config, "wechat", {"title": "标题", "body_markdown": "正文"}, account_id="wechat-a"
    )

    assert result["status"] == "failed"
    assert result["remote_id"] == "draft-id"
    assert "保留在草稿箱" in result["message"]
    assert "接口权限未开通" in result["message"]
    assert "结果不明确" not in result["message"]


def test_publish_api_reserves_each_account_and_records_partial_results(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "publish-api.sqlite3")
    database.migrate()
    config = AppConfig(database)
    runner = TaskRunner(database, config)
    article = _finalized_article(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [
        {"id": "wechat", "accounts": [{"id": "wechat-a", "name": "公众号甲", "configured": True}]},
        {"id": "zhihu", "accounts": [{"id": "zhihu-b", "name": "知乎乙", "configured": True}]},
    ])

    def fake_publish(_config, platform_id: str, _document: dict, *, account_id: str | None = None) -> dict:
        if platform_id == "zhihu":
            raise RuntimeError("知乎权限未开通")
        return {"status": "published", "remote_id": "wechat-article", "remote_url": "https://mp.weixin.qq.com/s/a", "message": "已确认发布"}

    monkeypatch.setattr(workbench_api, "publish_article_platform", fake_publish)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workbench_runner=runner)))
    background = BackgroundTasks()
    body = workbench_api.ArticlePublishRequest(
        targets=[
            workbench_api.ArticlePublishTarget(platform_id="wechat", account_id="wechat-a", options={"title": "公众号定制标题", "summary": "微信公众号摘要", "author": "老赵"}),
            workbench_api.ArticlePublishTarget(platform_id="zhihu", account_id="zhihu-b"),
        ],
        confirmed=True,
    )

    response = workbench_api.publish_article_endpoint(article["id"], body, request, background)

    assert [item["status"] for item in response["results"]] == ["publishing", "publishing"]
    assert len(background.tasks) == 2
    asyncio.run(background())
    updated = workbench_api.get_article(database, article["id"])
    latest = {(item["platform_id"], item["account_id"]): item for item in updated["publications"]}
    assert latest[("wechat", "wechat-a")]["status"] == "published"
    assert latest[("zhihu", "zhihu-b")]["status"] == "failed"
    assert latest[("wechat", "wechat-a")]["remote_url"] == "https://mp.weixin.qq.com/s/a"
    assert latest[("wechat", "wechat-a")]["options"] == {"title": "公众号定制标题", "summary": "微信公众号摘要", "author": "老赵"}


def test_publish_api_reuses_inflight_target_and_blocks_unknown_retry(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "publish-idempotency.sqlite3")
    database.migrate()
    runner = TaskRunner(database, AppConfig(database))
    article = _finalized_article(database)
    monkeypatch.setattr(workbench_api, "platform_statuses", lambda _config, **_kwargs: [
        {"id": "wechat", "accounts": [{"id": "wechat-a", "name": "公众号甲", "configured": True}]},
    ])
    first, _ = reserve_article_publication(database, article["id"], "wechat", article["version"], account_id="wechat-a", account_name="公众号甲")
    update_article_publication(database, first["id"], "unknown", message="请人工核对")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workbench_runner=runner)))
    body = workbench_api.ArticlePublishRequest(targets=[workbench_api.ArticlePublishTarget(platform_id="wechat", account_id="wechat-a")], confirmed=True)
    background = BackgroundTasks()

    response = workbench_api.publish_article_endpoint(article["id"], body, request, background)

    assert response["results"][0]["id"] == first["id"]
    assert response["results"][0]["status"] == "unknown"
    assert not background.tasks


def test_requested_fan_notification_moves_to_pending_only_after_publication(tmp_path: Path) -> None:
    database = Database(tmp_path / "wechat-notification-state.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, created = reserve_article_publication(
        database, article["id"], "wechat", article["version"],
        account_id="wechat-a", account_name="公众号甲", options={"notify_fans": True},
    )

    assert created is True
    assert publication["fan_notification_status"] == "waiting_publication"

    still_waiting = update_article_publication(database, publication["id"], "publishing", remote_id="publish-id")
    assert still_waiting["fan_notification_status"] == "waiting_publication"

    ready = update_article_publication(database, publication["id"], "published", remote_id="article-id")
    assert ready["fan_notification_status"] == "pending"
    assert ready["fan_notification_remote_id"] is None


def test_fan_notification_claim_is_atomic_and_interrupted_submit_is_not_retried(tmp_path: Path) -> None:
    database = Database(tmp_path / "wechat-notification-claim.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(
        database, article["id"], "wechat", article["version"],
        account_id="wechat-a", options={"notify_fans": True},
    )
    update_article_publication(database, publication["id"], "published", remote_id="article-id")

    claim_notification = getattr(article_studio, "claim_article_fan_notification", None)
    assert callable(claim_notification)
    claimed = claim_notification(database, publication["id"])
    repeated = claim_notification(database, publication["id"])

    assert claimed is not None
    assert claimed["fan_notification_status"] == "sending"
    assert repeated is None
    recovered = recover_article_publications(database)
    assert len(recovered) == 1
    assert recovered[0]["fan_notification_status"] == "unknown"
    assert "不要重发" in recovered[0]["fan_notification_message"]


def test_wechat_fan_notification_uses_separate_draft_and_all_followers_request(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-sendall.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda *_args: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_safe_article_cover", lambda _document: Path("cover.jpg"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_wechat_upload_cover", lambda *_args: "thumb-id")
    monkeypatch.setattr(article_platforms, "_wechat_prepare_body_images", lambda _document, _token, body, *_args: (body, []))
    calls: list[tuple[str, str, dict | None]] = []

    def fake_request(_app_id: str, _app_secret: str, url: str, *, method: str = "GET", body: dict | None = None) -> dict:
        calls.append((url, method, body))
        if "draft/add" in url:
            return {"media_id": "separate-notification-draft"}
        if "message/mass/sendall" in url:
            return {"msg_id": 123456789, "msg_data_id": 123}
        raise AssertionError(url)

    monkeypatch.setattr(article_platforms, "_wechat_json_request", fake_request)
    send_notification = getattr(article_platforms, "send_wechat_fan_notification", None)
    assert callable(send_notification)
    result = send_notification(
        config, "wechat-a", "publication-a",
        {"title": "标题", "summary": "摘要", "body_markdown": "正文"},
    )

    assert result["status"] == "sending"
    assert result["remote_id"] == "123456789"
    assert [call[0].split("?")[0].rsplit("/", 1)[-1] for call in calls] == ["add", "sendall"]
    assert calls[1][1] == "POST"
    payload = calls[1][2]
    assert payload is not None
    assert payload["filter"] == {"is_to_all": True}
    assert payload["msgtype"] == "mpnews"
    assert payload["mpnews"] == {"media_id": "separate-notification-draft"}
    assert len(payload["clientmsgid"]) == 32


def test_wechat_fan_notification_status_maps_official_states(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-mass-status.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda *_args: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    responses = iter([
        {"msg_id": "1", "msg_status": "SENDING"},
        {"msg_id": "2", "msg_status": "SEND_SUCCESS"},
        {"msg_id": "3", "msg_status": "SEND_FAIL"},
        {"msg_id": "4", "msg_status": "DELETE"},
    ])
    monkeypatch.setattr(article_platforms, "_wechat_json_request", lambda *_args, **_kwargs: next(responses))

    refresh_notification = getattr(article_platforms, "refresh_wechat_fan_notification", None)
    assert callable(refresh_notification)
    pending = refresh_notification(config, "wechat-a", "1")
    sent = refresh_notification(config, "wechat-a", "2")
    failed = refresh_notification(config, "wechat-a", "3")
    deleted = refresh_notification(config, "wechat-a", "4")

    assert pending["status"] == "sending"
    assert sent["status"] == "sent"
    assert failed["status"] == "failed"
    assert deleted["status"] == "failed"


def test_failed_wechat_publication_never_starts_requested_fan_notification(tmp_path: Path) -> None:
    database = Database(tmp_path / "wechat-publication-failed.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a",
        options={"notify_fans": True},
    )

    failed = update_article_publication(database, publication["id"], "failed", message="微信拒绝发表")
    claim_notification = getattr(article_studio, "claim_article_fan_notification")

    assert failed["fan_notification_status"] == "failed"
    assert "没有通知粉丝" in failed["fan_notification_message"]
    assert claim_notification(database, publication["id"]) is None


@pytest.mark.parametrize(
    ("error_code", "expected_message"),
    [(48001, "未授权调用群发接口"), (40164, "公网出口 IP")],
)
def test_wechat_mass_send_api_permission_error_is_not_reported_as_success(
    tmp_path: Path, monkeypatch, error_code: int, expected_message: str,
) -> None:
    database = Database(tmp_path / "wechat-mass-permission.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda *_args: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_safe_article_cover", lambda _document: Path("cover.jpg"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")
    monkeypatch.setattr(article_platforms, "_wechat_upload_cover", lambda *_args: "thumb-id")
    monkeypatch.setattr(article_platforms, "_wechat_prepare_body_images", lambda _document, _token, body, *_args: (body, []))

    def fake_request(_app_id: str, _app_secret: str, url: str, **_kwargs) -> dict:
        if "draft/add" in url:
            return {"media_id": "fan-draft"}
        raise article_platforms.PlatformResponseError("wechat permission error", error_code)

    monkeypatch.setattr(article_platforms, "_wechat_json_request", fake_request)
    result = article_platforms.send_wechat_fan_notification(
        config, "wechat-a", "publication-a", {"title": "标题", "body_markdown": "正文"},
    )

    assert result["status"] == "failed"
    assert result["remote_id"] is None
    assert expected_message in result["message"]


def test_wechat_mass_status_waits_for_admin_risk_confirmation(tmp_path: Path, monkeypatch) -> None:
    database = Database(tmp_path / "wechat-mass-risk-confirm.sqlite3")
    database.migrate()
    config = AppConfig(database)
    monkeypatch.setattr(article_platforms, "account_credentials", lambda *_args: ("wx_app_id", "secret"))
    monkeypatch.setattr(article_platforms, "_wechat_access_token", lambda *_args: "token")

    def fake_request(*_args, **_kwargs) -> dict:
        raise article_platforms.PlatformResponseError("approval pending", 89505)

    monkeypatch.setattr(article_platforms, "_wechat_json_request", fake_request)
    result = article_platforms.refresh_wechat_fan_notification(config, "wechat-a", "message-id")

    assert result["status"] == "sending"
    assert "管理员" in result["message"]


def test_only_explicit_failed_notification_without_message_id_can_be_retried(tmp_path: Path) -> None:
    database = Database(tmp_path / "wechat-notification-retry-state.sqlite3")
    database.migrate()
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a",
        options={"notify_fans": True},
    )
    update_article_publication(database, publication["id"], "published", remote_id="article-id")
    update_article_fan_notification(database, publication["id"], "failed", message="群发接口未授权")
    retry_notification = getattr(article_studio, "retry_article_fan_notification", None)
    assert callable(retry_notification)

    retried = retry_notification(database, publication["id"])
    assert retried is not None
    assert retried["fan_notification_status"] == "sending"

    update_article_fan_notification(database, publication["id"], "failed", remote_id="mass-id", message="发送失败")
    assert retry_notification(database, publication["id"]) is None


def test_retry_endpoint_requires_explicit_confirmation_and_dispatches_once(tmp_path: Path, monkeypatch) -> None:
    assert hasattr(workbench_api, "retry_article_fan_notification_endpoint")

    database = Database(tmp_path / "wechat-notification-retry-api.sqlite3")
    database.migrate()
    config = AppConfig(database)
    runner = TaskRunner(database, config)
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a",
        account_name="公众号甲", options={"notify_fans": True},
    )
    update_article_publication(database, publication["id"], "published", remote_id="article-id")
    update_article_fan_notification(database, publication["id"], "failed", message="群发接口未授权")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workbench_runner=runner)))
    calls: list[str] = []

    def fake_send(_config, _account_id: str, publication_id: str, _document: dict) -> dict:
        calls.append(publication_id)
        return {"status": "sending", "remote_id": "mass-id", "message": "群发已受理"}

    monkeypatch.setattr(workbench_api, "send_wechat_fan_notification", fake_send)
    background = BackgroundTasks()
    body = workbench_api.ArticleFanNotificationRetryRequest(confirmed=True)

    result = workbench_api.retry_article_fan_notification_endpoint(
        article["id"], publication["id"], body, request, background,
    )

    assert result["fan_notification_status"] == "sending"
    assert len(background.tasks) == 1
    asyncio.run(background())
    updated = workbench_api.get_article(database, article["id"])["publications"][0]
    assert updated["fan_notification_remote_id"] == "mass-id"
    assert calls == [publication["id"]]

    with pytest.raises(HTTPException) as rejected:
        workbench_api.retry_article_fan_notification_endpoint(
            article["id"], publication["id"], body, request, BackgroundTasks(),
        )
    assert rejected.value.status_code == 409


def test_publish_options_accept_per_request_fan_notification_flag() -> None:
    options = workbench_api.ArticlePublishOptions.model_validate({"notify_fans": True})

    assert options.notify_fans is True
    assert workbench_api.ArticlePublishOptions().notify_fans is False


def test_confirmed_wechat_publication_queues_fan_send_once_and_refreshes_its_result(tmp_path: Path, monkeypatch) -> None:
    assert "background_tasks" in inspect.signature(workbench_api.refresh_article_publication_endpoint).parameters

    database = Database(tmp_path / "wechat-notification-api.sqlite3")
    database.migrate()
    config = AppConfig(database)
    runner = TaskRunner(database, config)
    article = _finalized_article(database)
    publication, _ = reserve_article_publication(
        database, article["id"], "wechat", article["version"], account_id="wechat-a",
        account_name="公众号甲", options={"notify_fans": True},
    )
    update_article_publication(database, publication["id"], "publishing", remote_id="publish-id")
    monkeypatch.setattr(workbench_api, "refresh_wechat_publication", lambda *_args: {
        "status": "published", "remote_id": "article-id", "remote_url": "https://mp.weixin.qq.com/s/a", "message": "已发表",
    })
    sent_calls: list[tuple[str, str]] = []

    def fake_send(_config, account_id: str, publication_id: str, _document: dict) -> dict:
        sent_calls.append((account_id, publication_id))
        return {"status": "sending", "remote_id": "mass-message-id", "message": "群发已受理"}

    monkeypatch.setattr(workbench_api, "send_wechat_fan_notification", fake_send)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(workbench_runner=runner)))
    background = BackgroundTasks()

    first_refresh = workbench_api.refresh_article_publication_endpoint(
        article["id"], publication["id"], request, background,
    )

    assert first_refresh["status"] == "published"
    assert first_refresh["fan_notification_status"] == "sending"
    assert len(background.tasks) == 1
    asyncio.run(background())
    sent_record = workbench_api.get_article(database, article["id"])["publications"][0]
    assert sent_record["fan_notification_remote_id"] == "mass-message-id"
    assert sent_calls == [("wechat-a", publication["id"])]

    monkeypatch.setattr(workbench_api, "refresh_wechat_fan_notification", lambda *_args: {
        "status": "sent", "remote_id": "mass-message-id", "message": "粉丝通知已发送",
    })
    second_refresh = workbench_api.refresh_article_publication_endpoint(
        article["id"], publication["id"], request, BackgroundTasks(),
    )

    assert second_refresh["status"] == "published"
    assert second_refresh["fan_notification_status"] == "sent"
    assert second_refresh["fan_notification_message"] == "粉丝通知已发送"
