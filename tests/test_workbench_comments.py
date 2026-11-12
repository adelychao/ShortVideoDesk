from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from app.workbench.comments import douyin
from app.workbench.database import Database
from app.workbench.models import CommentPage, CommentRecord, SourceKind, SourceRecord, StepKind, StepRecord, TaskRecord
from app.workbench.steps import comments as comments_step_module
from app.workbench.steps.comments import _choose_note_target, _mark_analysis_stale, _write_comment_note


def test_comment_note_retries_preserve_previous_versions(tmp_path: Path) -> None:
    (tmp_path / "视频资料.md").write_text("first", encoding="utf-8")
    (tmp_path / "视频资料-评论-task-com.md").write_text("second", encoding="utf-8")
    target = _choose_note_target(tmp_path, "task-comments")
    assert not target.exists()
    assert target.parent == tmp_path


def test_new_comments_mark_existing_analysis_stale_without_queuing(tmp_path: Path) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id, identity_key, source_kind, created_at) VALUES ('a', 'a', 'url', ?)", (now,))
        connection.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t', 'a', '{}', 'running', ?, ?)", (now, now))
        connection.execute("""INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json)
                              VALUES ('s', 't', 'analyze_comments', 2, 'completed', '[]', '[]', '[]')""")
    _mark_analysis_stale(database, "t")
    with database.transaction() as connection:
        row = connection.execute("SELECT status, message, error_code FROM task_steps WHERE id = 's'").fetchone()
    assert row["status"] == "interrupted"
    assert row["error_code"] == "comments_analysis_stale"
    assert "重新授权" in row["message"]


@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_retryable_comment_failure_retries_once_without_waiting_for_other_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_succeeds: bool
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    now = datetime.now(timezone.utc)
    source = SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123")
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id, identity_key, source_kind, platform, source_id, created_at) VALUES ('a', 'a', 'url', 'douyin', '123', ?)", (now.isoformat(),))
        connection.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t', 'a', ?, 'running', ?, ?)", (json.dumps(source.to_dict()), now.isoformat(), now.isoformat()))
        connection.execute("INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json) VALUES ('c', 't', 'comments', 1, 'running', '[]', '[]', '[]')")
    attempts = []
    progress = []

    class Adapter:
        def __init__(self, attempt):
            self.attempt = attempt
        def fetch_page(self, _source, cursor):
            assert cursor is None
            if self.attempt == 1 or not retry_succeeds:
                raise comments_step_module.CommentAdapterError("comments_access_limited", "temporarily unavailable", retryable=True)
            comment = CommentRecord(id="retry-comment", platform_comment_id="retry-comment", parent_id=None, author=None, text="成功重试后获取的评论", like_count=1, created_at=None, collected_at=now)
            return CommentPage([comment], None, "平台已返回末页", False)
        def close(self):
            pass

    def make_adapter(_platform):
        attempts.append(1)
        return Adapter(len(attempts))

    monkeypatch.setattr(comments_step_module, "adapter_for", make_adapter)
    monkeypatch.setattr(comments_step_module, "_write_comment_note", lambda *_args: "note")
    context = SimpleNamespace(
        repository=SimpleNamespace(database=database),
        task=TaskRecord(id="t", asset_id="a", source=source, status="running", created_at=now, updated_at=now),
        step=StepRecord(id="c", kind=StepKind.COMMENTS, ordinal=1, status="running"),
        archive_root=tmp_path, cancel_event=threading.Event(),
        progress=lambda _step, _percent, message: progress.append(message),
    )
    if retry_succeeds:
        result = comments_step_module.collect_comments(context)
        assert result.status == "completed"
    else:
        with pytest.raises(comments_step_module.RunnerError, match="temporarily unavailable"):
            comments_step_module.collect_comments(context)
    assert len(attempts) == 2
    assert any("自动重试" in message for message in progress)
    with database.transaction() as connection:
        retry_count = connection.execute("SELECT retry_count FROM task_steps WHERE id='c'").fetchone()["retry_count"]
    assert retry_count == 1


@pytest.mark.parametrize("new_comment, expected_status", [(False, "completed"), (True, "interrupted")])
def test_resuming_collection_invalidates_analysis_only_when_data_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_comment: bool, expected_status: str
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    now = datetime.now(timezone.utc)
    source = SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123")
    with database.transaction(write=True) as connection:
        connection.execute("INSERT INTO assets (id, identity_key, source_kind, platform, source_id, created_at) VALUES ('a', 'a', 'url', 'douyin', '123', ?)", (now.isoformat(),))
        connection.execute("INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at) VALUES ('t', 'a', ?, 'running', ?, ?)", (json.dumps(source.to_dict()), now.isoformat(), now.isoformat()))
        connection.execute("""INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json)
                              VALUES ('c', 't', 'comments', 1, 'running', '[]', '[]', '[]')""")
        connection.execute("""INSERT INTO task_steps (id, task_id, kind, ordinal, status, depends_on_json, input_artifact_ids_json, output_artifact_ids_json)
                              VALUES ('s', 't', 'analyze_comments', 2, 'completed', '[]', '[]', '[]')""")
        connection.execute("""INSERT INTO comment_collection_state (task_id, asset_id, next_cursor, page_count, collected_count, status, updated_at)
                              VALUES ('t', 'a', '5', 1, 0, 'partial', ?)""", (now.isoformat(),))
    class Adapter:
        def fetch_page(self, _source, cursor):
            assert cursor == "5"
            comments = [CommentRecord(id="new", platform_comment_id="new", parent_id=None, author=None, text="new", like_count=1, created_at=None, collected_at=now)] if new_comment else []
            return CommentPage(comments, None, "平台已返回末页", False)
        def close(self):
            pass
    monkeypatch.setattr(comments_step_module, "adapter_for", lambda _: Adapter())
    monkeypatch.setattr(comments_step_module, "_write_comment_note", lambda *_: "note")
    context = SimpleNamespace(repository=SimpleNamespace(database=database), task=TaskRecord(id="t", asset_id="a", source=source, status="running", created_at=now, updated_at=now), step=StepRecord(id="c", kind=StepKind.COMMENTS, ordinal=1, status="running"), archive_root=tmp_path, cancel_event=threading.Event(), progress=lambda *_: None)
    comments_step_module.collect_comments(context)
    with database.transaction() as connection:
        status = connection.execute("SELECT status FROM task_steps WHERE id = 's'").fetchone()["status"]
    assert status == expected_status


def test_douyin_comment_capture_accepts_hybrid_api_host(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status_code": 0, "comments": [], "cursor": 0, "has_more": 0}
    response = Mock(
        status=200,
        url=(
            "https://www-hj.douyin.com/aweme/v1/web/comment/list/"
            "?aweme_id=7699453390744489226"
        ),
    )
    response.json.return_value = payload

    class FakePage:
        def __init__(self) -> None:
            self.handlers = {}
            self.route_handler = None
            self.url = None

        def on(self, event: str, callback) -> None:
            self.handlers[event] = callback

        def route(self, _url, callback) -> None:
            self.route_handler = callback

        def goto(self, url, **_kwargs) -> None:
            self.url = url
            if "response" in self.handlers:
                self.handlers["response"](response)
            else:
                self.route_handler(Mock(fulfill=Mock()))

        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

    page = FakePage()
    browser = Mock()
    context = Mock()
    context.new_page.return_value = page
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    browser.new_context.return_value = context
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    times = iter((0.0, 19.0))
    monkeypatch.setattr(douyin.time, "monotonic", lambda: next(times))

    adapter = douyin.DouyinCommentAdapter()
    try:
        adapter._ensure_browser(
            SourceRecord(
                kind=SourceKind.URL,
                value="https://www.douyin.com/video/7699453390744489226",
                platform="douyin",
                source_id="7699453390744489226",
            )
        )
        assert adapter._initial == payload
        assert adapter._api_origin == "https://www-hj.douyin.com"
        assert page.url == "https://www.douyin.com/video/7699453390744489226"
    finally:
        adapter.close()


@pytest.mark.parametrize("capture_after_timeout", [False, True])
def test_douyin_navigation_timeout_keeps_matching_page_and_captured_comments(
    monkeypatch: pytest.MonkeyPatch, capture_after_timeout: bool
) -> None:
    payload = {"status_code": 0, "comments": [], "cursor": 0, "has_more": 0}
    response = Mock(status=200, url="https://www-hj.douyin.com/aweme/v1/web/comment/list/?aweme_id=123")
    response.json.return_value = payload

    class Page:
        url = "https://www.douyin.com/video/123"
        def is_closed(self):
            return False
        def locator(self, _selector):
            return Mock(inner_text=Mock(return_value="作品页面"))
        def on(self, _event, callback):
            self.callback = callback
        def goto(self, *_args, **_kwargs):
            if not capture_after_timeout:
                self.callback(response)
            raise PlaywrightTimeoutError("navigation timeout")
        def wait_for_timeout(self, _milliseconds):
            self.callback(response)
    page = Page()
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    adapter = douyin.DouyinCommentAdapter()
    try:
        adapter._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
        assert adapter._initial == payload
        assert adapter._page is page
    finally:
        adapter.close()


@pytest.mark.parametrize("url", ["about:blank", "https://www.douyin.com/video/999", "https://example.com/video/123"])
def test_douyin_navigation_timeout_rejects_unverified_page(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    page = Mock(url=url)
    page.goto.side_effect = PlaywrightTimeoutError("navigation timeout")
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    adapter = douyin.DouyinCommentAdapter()
    with pytest.raises(douyin.CommentAdapterError) as error:
        adapter._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
    assert error.value.code == "comments_navigation_timeout"
    context.close.assert_called_once()


def test_douyin_browser_start_failure_is_distinguished(monkeypatch: pytest.MonkeyPatch) -> None:
    playwright = Mock()
    playwright.chromium.launch.side_effect = douyin.PlaywrightError("browser failure")
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    adapter = douyin.DouyinCommentAdapter()
    with pytest.raises(douyin.CommentAdapterError) as error:
        adapter._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
    assert error.value.code == "comments_browser_start_failed"
    playwright.stop.assert_called_once()


def test_douyin_page_timeout_without_comments_remains_navigation_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    page = Mock(url="https://www.douyin.com/video/123")
    page.is_closed.return_value = False
    page.locator.return_value.inner_text.return_value = "作品页面"
    page.goto.side_effect = PlaywrightTimeoutError("navigation timeout")
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    ticks = iter((0.0, 19.0))
    monkeypatch.setattr(douyin.time, "monotonic", lambda: next(ticks))
    with pytest.raises(douyin.CommentAdapterError) as error:
        douyin.DouyinCommentAdapter()._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
    assert error.value.code == "comments_navigation_timeout"


def test_douyin_page_timeout_does_not_accept_verification_page(monkeypatch: pytest.MonkeyPatch) -> None:
    page = Mock(url="https://www.douyin.com/video/123")
    page.is_closed.return_value = False
    page.locator.return_value.inner_text.return_value = "请完成下列验证"
    page.goto.side_effect = PlaywrightTimeoutError("navigation timeout")
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    with pytest.raises(douyin.CommentAdapterError) as error:
        douyin.DouyinCommentAdapter()._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
    assert error.value.code == "comments_navigation_timeout"


def test_douyin_navigation_error_is_not_treated_as_a_recoverable_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    page = Mock(url="https://www.douyin.com/video/123")
    page.goto.side_effect = douyin.PlaywrightError("navigation failed")
    context = Mock(new_page=Mock(return_value=page))
    browser = Mock(new_context=Mock(return_value=context))
    playwright = Mock(chromium=Mock(launch=Mock(return_value=browser)))
    monkeypatch.setattr(douyin, "sync_playwright", lambda: Mock(start=Mock(return_value=playwright)))
    with pytest.raises(douyin.CommentAdapterError) as error:
        douyin.DouyinCommentAdapter()._ensure_browser(SourceRecord(kind=SourceKind.URL, value="https://www.douyin.com/video/123", platform="douyin", source_id="123"))
    assert error.value.code == "comments_navigation_failed"
    context.close.assert_called_once()


def test_douyin_reply_collection_stops_when_api_cursor_cycles() -> None:
    adapter = douyin.DouyinCommentAdapter()
    adapter._page = Mock()
    pages = [
        {"comments": [], "has_more": 1, "cursor": "a"},
        {"comments": [], "has_more": 1, "cursor": "b"},
        {"comments": [], "has_more": 1, "cursor": "a"},
        {"comments": [], "has_more": 0, "cursor": "c"},
    ]
    with patch.object(adapter, "_request", side_effect=pages) as request:
        adapter._reply_records("7699453390744489226", {"cid": "root", "reply_comment_total": 100})

    assert request.call_count == 3
    assert adapter._reply_warning is not None
    assert "游标重复" in adapter._reply_warning


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"state": "error"}, "未收到有效响应"),
        ({"state": "done", "response": {"status": 403, "text": ""}}, "平台拒绝"),
    ],
)
def test_reply_warning_distinguishes_no_response_from_refusal(result, expected) -> None:
    adapter = douyin.DouyinCommentAdapter()
    adapter._page = Mock()
    adapter._page.evaluate.side_effect = [None, result]

    replies = adapter._reply_records("123", {"cid": "root", "reply_comment_total": 2})

    assert replies == []
    assert expected in adapter._reply_warning
    if expected == "未收到有效响应":
        assert "平台拒绝" not in adapter._reply_warning


def test_douyin_comment_collection_skips_replies_by_default() -> None:
    adapter = douyin.DouyinCommentAdapter()
    adapter._page = Mock()
    adapter._initial = {
        "comments": [
            {
                "cid": "root",
                "text": "主评论",
                "user": {"nickname": "作者"},
                "reply_comment_total": 1,
            }
        ],
        "has_more": 0,
        "cursor": 0,
    }
    adapter._request = Mock(return_value={"comments": [], "has_more": 0, "cursor": 0})
    adapter._ensure_browser = Mock()
    progress: list[str] = []
    adapter.set_progress_callback(progress.append)

    result = adapter.fetch_page(
        SourceRecord(
            kind=SourceKind.URL,
            value="https://www.douyin.com/video/7699453390744489226",
            platform="douyin",
            source_id="7699453390744489226",
        ),
        None,
    )

    assert [comment.platform_comment_id for comment in result.comments] == ["root"]
    adapter._request.assert_not_called()
    assert not progress


def test_douyin_api_requests_use_the_loaded_page_and_return_valid_data() -> None:
    adapter = douyin.DouyinCommentAdapter()
    original_page = Mock()
    isolated_page = Mock()
    original_page.evaluate.side_effect = [None, {
        "state": "done", "response": {
            "status": 200,
            "text": '{"status_code":0,"comments":[],"has_more":0}',
        },
    }]
    adapter._page = original_page
    adapter._api_page = isolated_page
    adapter._api_origin = "https://www-hj.douyin.com"

    payload = adapter._request("/aweme/v1/web/comment/list/reply/", {"cursor": 0})

    assert payload["status_code"] == 0
    assert original_page.evaluate.call_count == 2
    isolated_page.evaluate.assert_not_called()


def test_douyin_reply_failure_does_not_skip_other_roots() -> None:
    adapter = douyin.DouyinCommentAdapter()
    adapter._page = Mock()
    with patch.object(adapter, "_request", side_effect=[
        douyin.CommentAdapterError("comments_access_limited", "暂时不可用"),
        {"comments": [{"cid": "reply", "text": "回复"}], "has_more": 0},
    ]):
        assert adapter._reply_records("123", {"cid": "first", "reply_comment_total": 1}) == []
        replies = adapter._reply_records("123", {"cid": "second", "reply_comment_total": 1})
    assert [reply.platform_comment_id for reply in replies] == ["reply"]
    assert adapter._reply_warning is not None


def test_douyin_comment_fetch_aborts_when_server_never_finishes() -> None:
    from app.browser_safety import browser_automation_paused
    if browser_automation_paused():
        pytest.skip("Windows browser automation is paused pending safety verification")
    from playwright.sync_api import sync_playwright

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/hang":
                time.sleep(1.2)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True, timeout=10_000)
            context = browser.new_context()
            page = context.new_page()
            page.goto(origin + "/start", wait_until="domcontentloaded")
            try:
                outcome = page.evaluate(
                    """async ({script, args}) => {
                      const run = new Function(`return (${script})`)();
                      return await Promise.race([
                        run(args).then(() => "resolved", error => error.name),
                        new Promise(resolve => setTimeout(() => resolve("still_waiting"), 500))
                      ]);
                    }""",
                    {
                        "script": douyin._PAGE_SCRIPT,
                        "args": {
                            "endpoint": origin + "/hang",
                            "params": {},
                            "timeout_ms": 100,
                        },
                    },
                )
                assert outcome == "AbortError"
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


@pytest.mark.parametrize("reply_warning, expected_status", [
    ("部分楼中楼受到平台限制；已保留可访问的主评论和此前采集内容", "completed"),
    (None, "completed"),
])
def test_reply_warning_does_not_make_complete_main_comment_pages_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply_warning: str | None, expected_status: str
) -> None:
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    task_id, asset_id, step_id = "task-comments", "asset-comments", "step-comments"
    source = SourceRecord(
        kind=SourceKind.URL,
        value="https://www.douyin.com/video/7699453390744489226",
        platform="douyin",
        source_id="7699453390744489226",
    )
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO assets (id, identity_key, source_kind, platform, source_id, created_at)
               VALUES (?, ?, 'url', 'douyin', ?, ?)""",
            (asset_id, "douyin:comments-test", source.source_id, now_text),
        )
        connection.execute(
            """INSERT INTO tasks (id, asset_id, source_json, status, created_at, updated_at)
               VALUES (?, ?, ?, 'running', ?, ?)""",
            (task_id, asset_id, json.dumps(source.to_dict()), now_text, now_text),
        )
        connection.execute(
            """INSERT INTO task_steps (
                   id, task_id, kind, ordinal, status, depends_on_json,
                   input_artifact_ids_json, output_artifact_ids_json
               ) VALUES (?, ?, 'comments', 1, 'running', '[]', '[]', '[]')""",
            (step_id, task_id),
        )

    def comment(comment_id: str, text: str) -> CommentRecord:
        return CommentRecord(
            id=f"record-{comment_id}",
            platform_comment_id=comment_id,
            parent_id=None,
            author=None,
            text=text,
            like_count=1,
            created_at=None,
            collected_at=now,
        )

    class FakeAdapter:
        def __init__(self) -> None:
            self.cursors: list[str | None] = []

        def fetch_page(self, _source: SourceRecord, cursor: str | None) -> CommentPage:
            self.cursors.append(cursor)
            if cursor is None:
                return CommentPage([comment("root-1", "第一页主评论")], "5", None, True, reply_warning=reply_warning)
            return CommentPage([comment("root-2", "第二页主评论")], None, "平台已返回末页", False, reply_warning=reply_warning)

        def close(self) -> None:
            pass

    adapter = FakeAdapter()
    monkeypatch.setattr(comments_step_module, "adapter_for", lambda _platform: adapter)
    monkeypatch.setattr(comments_step_module, "_write_comment_note", lambda *_args: "note-id")
    context = SimpleNamespace(
        repository=SimpleNamespace(database=database),
        task=TaskRecord(
            id=task_id,
            asset_id=asset_id,
            source=source,
            status="running",
            created_at=now,
            updated_at=now,
        ),
        step=StepRecord(id=step_id, kind=StepKind.COMMENTS, ordinal=1, status="running"),
        archive_root=tmp_path / "archive",
        cancel_event=threading.Event(),
        progress=lambda *_args: None,
    )

    result = comments_step_module.collect_comments(context)

    assert result.status == expected_status
    assert adapter.cursors == [None, "5"]
    with database.transaction() as connection:
        state = connection.execute(
            "SELECT page_count, collected_count, next_cursor, stop_reason FROM comment_collection_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert state["page_count"] == 2
    assert state["collected_count"] == 2
    assert state["next_cursor"] is None
    assert state["stop_reason"] == (f"平台已返回末页；{reply_warning}" if reply_warning else "平台已返回末页")


def test_comment_note_uses_parent_when_archive_path_is_media_file(tmp_path: Path) -> None:
    archive_root = tmp_path / "archive"
    media_folder = archive_root / "2026-10-24" / "0001_demo"
    media_folder.mkdir(parents=True)
    video_path = media_folder / "video.mp4"
    video_path.write_bytes(b"video")
    database = Database(tmp_path / "workbench.sqlite3")
    database.migrate()
    now = datetime.now(timezone.utc).isoformat()
    task_id, asset_id, step_id = "task-1", "asset-1", "step-1"
    source = SourceRecord(
        kind=SourceKind.URL,
        value="https://www.douyin.com/video/7699453390744489226",
        platform="douyin",
        source_id="7699453390744489226",
        title="示例作品",
    )
    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO assets (
                   id, identity_key, source_kind, platform, source_id, title,
                   archive_path, created_at
               ) VALUES (?, ?, 'url', 'douyin', ?, ?, ?, ?)""",
            (asset_id, "douyin:7699453390744489226", source.source_id, source.title, str(video_path), now),
        )
        connection.execute(
            """INSERT INTO tasks (
                   id, asset_id, source_json, status, created_at, updated_at
               ) VALUES (?, ?, ?, 'running', ?, ?)""",
            (task_id, asset_id, json.dumps(source.to_dict()), now, now),
        )
        connection.execute(
            """INSERT INTO task_steps (
                   id, task_id, kind, ordinal, status, depends_on_json,
                   input_artifact_ids_json, output_artifact_ids_json
               ) VALUES (?, ?, 'comments', 1, 'running', '[]', '[]', '[]')""",
            (step_id, task_id),
        )
        connection.execute(
            """INSERT INTO comment_collection_state (
                   task_id, asset_id, page_count, collected_count,
                   missing_fields_json, stop_reason, status, updated_at, sort_order
               ) VALUES (?, ?, 1, 5, '{}', ?, 'partial', ?, 'likes_desc')""",
            (task_id, asset_id, "平台限制", now),
        )
        connection.execute(
            """INSERT INTO comments (
                   id, asset_id, task_id, platform_comment_id, parent_id, author,
                   text, like_count, created_at, collected_at, raw_fields_json
               ) VALUES ('comment-1', ?, ?, '7699516712375272246', NULL, ?, ?, 59, ?, ?, '{}')""",
                (asset_id, task_id, "示例作者", "这是一条留言内容", "2026-10-22T15:37:14+00:00", now),
        )
        connection.execute(
            """INSERT INTO comment_collection_items (task_id, comment_id, collected_at)
               VALUES (?, 'comment-1', ?)""",
            (task_id, now),
        )

    task = TaskRecord(
        id=task_id,
        asset_id=asset_id,
        source=source,
        status="running",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    step = StepRecord(id=step_id, kind=StepKind.COMMENTS, ordinal=1, status="running")
    context = SimpleNamespace(
        repository=SimpleNamespace(database=database),
        task=task,
        step=step,
        archive_root=archive_root,
        cancel_event=threading.Event(),
    )

    artifact_id = _write_comment_note(context, "平台限制")

    with database.transaction() as connection:
        artifact = connection.execute("SELECT path, sha256 FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    note_path = Path(artifact["path"])
    assert note_path.parent == media_folder
    assert note_path.is_file()
    assert hashlib.sha256(note_path.read_bytes()).hexdigest() == artifact["sha256"]
    note_text = note_path.read_text(encoding="utf-8")
    assert "本次新增：5 条" in note_text
    assert "这是一条留言内容" in note_text
    assert "点赞：59" in note_text
    assert "示例作者" not in note_text
    assert "2026-10-22T15:37:14+00:00" not in note_text
    assert "父评论 ID：" not in note_text
    assert "### 评论 7699516712375272246" not in note_text
