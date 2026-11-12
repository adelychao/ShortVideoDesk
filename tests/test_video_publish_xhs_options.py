from types import SimpleNamespace

import pytest

from app.workbench.api import VideoPublishOptionsRequest
from app.workbench.database import Database
from app.workbench import video_publishing as publish


def test_xhs_options_are_accepted_and_passed_to_publisher():
    options = VideoPublishOptionsRequest(xhs_original=True, xhs_declaration="ai", xhs_visibility="private", xhs_location="人民公园").model_dump(exclude_none=True)
    adapter = publish.SocialAutoUploadAdapter(None)
    calls = []
    adapter.run = lambda arguments, on_start, timeout: calls.append(arguments) or publish.SauResult(0, "")
    adapter.upload({"platform_id": "xiaohongshu", "account_alias": "user", "media_path": "video.mp4", "title": "标题", "description": "", "tags": [], "options": options}, lambda process: None)
    args = calls[0]
    for flag, value in (("--xhs-declaration", "ai"), ("--xhs-visibility", "private"), ("--xhs-location", "人民公园")):
        assert args[args.index(flag) + 1] == value
    assert "--xhs-original" in args
    assert ("--headed" in args) == (publish.os.name == "nt")


def test_xhs_options_survive_batch_and_claim(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','xiaohongshu','user','connected','2026-11-02','2026-11-02')")
    media = tmp_path / "video.mp4"
    media.write_bytes(b"test")
    monkeypatch.setattr(publish, "get_upload", lambda *a, **kw: SimpleNamespace(path=media, filename="video.mp4", sha256="hash", detected_media_type="video"))
    monkeypatch.setattr(publish, "_copy_video_to_managed_storage", lambda *a: media)
    monkeypatch.setattr(publish, "delete_upload", lambda *a: None)
    options = {"xhs_original": True, "xhs_declaration": "ai", "xhs_visibility": "private", "xhs_location": "人民公园", "xhs_location_id": "B123", "xhs_location_address": "上海市公园路"}
    batch = publish.create_video_publish_batch(db, archive_root=None, idempotency_key="1234567890abcdef", asset_id=None, upload_id="u", title="标题", description="", tags=[], schedule_at=None, targets=[{"account_id": "a", "options": options}])
    claimed = publish._claim_video_publish_target(db, batch["targets"][0]["id"])
    assert all(claimed["options"][key] == value for key, value in options.items())


def test_xhs_verification_progress_is_shown_even_after_recent_output(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.migrate()
    with db.transaction(write=True) as c:
        c.execute("INSERT INTO video_publish_accounts(id,platform_id,account_alias,status,created_at,updated_at) VALUES('a','xiaohongshu','user','connected','t','t')")
        c.execute("INSERT INTO video_publish_batches(id,idempotency_key,media_path,media_name,media_size,media_sha256,title,status,created_at,updated_at) VALUES('b','unique','unused','video.mp4',5,'x','title','running','t','t')")
        c.execute("INSERT INTO video_publish_targets(id,batch_id,account_id,platform_id,account_alias,status,created_at,updated_at) VALUES('t','b','a','xiaohongshu','user','running','t','t')")
    manager = publish.VideoPublishManager(db)
    manager._record_publish_output('t', '刚上传完成')
    manager._record_publish_output('t', 'XHS_VIDEO_VERIFY:461\n')
    target = publish.get_video_publish_target(db, 't')
    assert target['status'] == 'running'
    assert 'HTTP 461' in target['message'] and '窗口' in target['message'] and '验证并提交' in target['message']


def test_repost_declaration_requires_source():
    with pytest.raises(publish.VideoPublishError):
        publish.validate_xhs_options({"xhs_declaration": "repost"})


def test_poi_selection_requires_name_id_and_address():
    with pytest.raises(publish.VideoPublishError):
        publish.validate_xhs_options({"xhs_location": "公园", "xhs_location_id": "B123"})
    publish.validate_xhs_options({"xhs_location": "公园", "xhs_location_id": "B123", "xhs_location_address": "上海市公园路"})


def test_publish_rejects_location_if_platform_identity_changed(tmp_path, monkeypatch):
    from app.workbench import xhs_poi
    adapter = publish.SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, 'resolve_command', lambda: (publish.SauCommand(('sau',), tmp_path, str(tmp_path)), ''))
    monkeypatch.setattr(xhs_poi, 'search_poi', lambda *args: [{'id': 'B999', 'name': '公园', 'address': '上海市公园路'}])
    adapter.run = lambda *args: pytest.fail('must not publish a mismatched location')
    target = {'platform_id': 'xiaohongshu', 'account_alias': 'user', 'media_path': 'video.mp4', 'title': '标题', 'description': '', 'tags': [], 'options': {'xhs_location': '公园', 'xhs_location_id': 'B123', 'xhs_location_address': '上海市公园路'}}
    assert '地点' in adapter.upload(target, lambda process: None).launch_error


def test_extended_xhs_publish_uses_project_python_and_keeps_login_command(tmp_path, monkeypatch):
    project = tmp_path / "sau"
    extension = project / "uploader" / "xiaohongshu_uploader" / "workbench_video.py"
    extension.parent.mkdir(parents=True)
    python = project / ".venv" / ("Scripts/python.exe" if publish.os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    adapter = publish.SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, "resolve_command", lambda: (publish.SauCommand(("sau",), project, str(project)), ""))
    monkeypatch.setattr(publish, "require_safe_browser", lambda operation: None)
    monkeypatch.setattr(publish, "require_controlled_video_browser", lambda project: None)
    calls = []
    class FakeProcess:
        returncode = 0
        def __init__(self, args, **kwargs):
            calls.append(args)
        def communicate(self, timeout):
            return "ok", ""
    monkeypatch.setattr(publish.subprocess, "Popen", FakeProcess)
    missing = adapter._run_command(["xiaohongshu", "upload-video", "--headed"], lambda p: None, 1, interactive_terminal=False, xhs_login=False)
    assert missing.launch_error and "发布扩展" in missing.launch_error
    assert not calls
    extension.touch()
    adapter._run_command(["xiaohongshu", "upload-video", "--xhs-visibility", "private"], lambda p: None, 1, interactive_terminal=False, xhs_login=False)
    adapter._run_command(["xiaohongshu", "check", "--account", "user"], lambda p: None, 1, interactive_terminal=False, xhs_login=False)
    if publish.os.name == 'nt':
        assert calls[0][0] == str(python)
        assert calls[0][1].endswith('safe_video_runner.py')
        assert calls[0][2:4] == ['xiaohongshu', 'upload-video']
    else:
        assert calls[0][:3] == [str(python), "-m", "uploader.xiaohongshu_uploader.workbench_video"]
    assert calls[0][-2:] == ["--xhs-visibility", "private"]
    assert calls[1][:3] == ["sau", "xiaohongshu", "check"]
