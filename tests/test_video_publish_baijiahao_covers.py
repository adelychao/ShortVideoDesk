from types import SimpleNamespace

import pytest

from app.workbench import video_publishing as publish
from app.workbench.database import Database


def _baijiahao_options(**covers):
    return {
        "baijiahao_declaration": "个人观点，仅供参考",
        **covers,
    }


def test_baijiahao_target_options_accept_3_4_and_16_9_cover_pair():
    options = _baijiahao_options(
        cover_portrait_upload_id="a" * 32,
        cover_16_9_upload_id="b" * 32,
    )

    normalized = publish._normalize_video_publish_target_options(
        platform_id="baijiahao",
        schedule_at=None,
        options=options,
        common_title="视频标题",
        common_description="",
        common_tags=[],
    )

    assert normalized["cover_portrait_upload_id"] == "a" * 32
    assert normalized["cover_16_9_upload_id"] == "b" * 32


def test_baijiahao_rejects_incomplete_new_cover_pair():
    with pytest.raises(publish.VideoPublishError) as raised:
        publish._normalize_video_publish_target_options(
            platform_id="baijiahao",
            schedule_at=None,
            options=_baijiahao_options(cover_16_9_upload_id="a" * 32),
            common_title="视频标题",
            common_description="",
            common_tags=[],
        )

    assert raised.value.code == "baijiahao_cover_pair_required"


def test_baijiahao_legacy_single_cover_option_remains_accepted():
    normalized = publish._normalize_video_publish_target_options(
        platform_id="baijiahao",
        schedule_at=None,
        options=_baijiahao_options(cover_upload_id="c" * 32),
        common_title="视频标题",
        common_description="",
        common_tags=[],
    )

    assert normalized["cover_upload_id"] == "c" * 32


def test_baijiahao_batch_archives_3_4_and_16_9_cover_files(tmp_path, monkeypatch):
    database = Database(tmp_path / "video-publish.sqlite3")
    database.migrate()
    with database.transaction(write=True) as connection:
        connection.execute(
            "INSERT INTO video_publish_accounts "
            "(id, platform_id, account_alias, status, created_at, updated_at) "
            "VALUES ('account', 'baijiahao', 'creator', 'connected', '2026-11-08', '2026-11-08')"
        )

    media = tmp_path / "video.mp4"
    media.write_bytes(b"video fixture")
    monkeypatch.setattr(
        publish,
        "get_upload",
        lambda *args, **kwargs: SimpleNamespace(path=media, filename="video.mp4", sha256="hash", detected_media_type="video"),
    )
    monkeypatch.setattr(publish, "_copy_video_to_managed_storage", lambda *args: media)
    monkeypatch.setattr(publish, "delete_upload", lambda *args: None)
    monkeypatch.setattr(publish, "get_video_publish_cover_upload", lambda upload_id: SimpleNamespace(size_bytes=1))
    monkeypatch.setattr(
        publish,
        "_copy_video_publish_cover_to_batch",
        lambda cover, batch_id, upload_id: f"{upload_id}.jpg",
    )
    monkeypatch.setattr(publish, "delete_video_publish_cover_upload", lambda *args: None)

    batch = publish.create_video_publish_batch(
        database,
        archive_root=None,
        idempotency_key="1234567890abcdef",
        asset_id=None,
        upload_id="upload",
        title="视频标题",
        description="",
        tags=[],
        schedule_at=None,
        targets=[{
            "account_id": "account",
            "options": _baijiahao_options(
                cover_portrait_upload_id="a" * 32,
                cover_16_9_upload_id="b" * 32,
            ),
        }],
    )

    saved = batch["targets"][0]["options"]
    assert saved["cover_portrait_filename"] == f"{'a' * 32}.jpg"
    assert saved["cover_16_9_filename"] == f"{'b' * 32}.jpg"
    assert saved["cover_filename"] == saved["cover_16_9_filename"]


def test_baijiahao_adapter_passes_both_cover_paths_to_sau(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SauCommand, SauResult, SocialAutoUploadAdapter

    media = tmp_path / "video.mp4"
    cover_3_4 = tmp_path / "cover-3x4.jpg"
    cover_16_9 = tmp_path / "cover-16x9.jpg"
    media.write_bytes(b"video fixture")
    cover_3_4.write_bytes(b"3:4 cover")
    cover_16_9.write_bytes(b"16:9 cover")
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, "resolve_command", lambda: (SauCommand(("sau",), tmp_path, str(tmp_path)), ""))
    captured = []
    monkeypatch.setattr(adapter, "run", lambda argv, on_start, timeout: captured.extend(argv) or SauResult(0, ""))

    result = adapter.upload({
        "platform_id": "baijiahao",
        "account_alias": "creator",
        "media_path": str(media),
        "title": "视频标题",
        "description": "",
        "tags": [],
        "options": {
            "baijiahao_declaration": "个人观点，仅供参考",
            "cover_filename": cover_16_9.name,
            "cover_portrait_filename": cover_3_4.name,
            "cover_16_9_filename": cover_16_9.name,
        },
    }, lambda _process: None)

    assert result.returncode == 0
    assert captured[captured.index("--thumbnail-3-4") + 1] == str(cover_3_4.resolve())
    assert captured[captured.index("--thumbnail-16-9") + 1] == str(cover_16_9.resolve())


def test_baijiahao_adapter_keeps_legacy_single_thumbnail_flag(tmp_path, monkeypatch):
    from app.workbench.video_publishing import SauCommand, SauResult, SocialAutoUploadAdapter

    media = tmp_path / "video.mp4"
    cover = tmp_path / "cover.jpg"
    media.write_bytes(b"video fixture")
    cover.write_bytes(b"cover fixture")
    adapter = SocialAutoUploadAdapter(None)
    monkeypatch.setattr(adapter, "resolve_command", lambda: (SauCommand(("sau",), tmp_path, str(tmp_path)), ""))
    captured = []
    monkeypatch.setattr(adapter, "run", lambda argv, on_start, timeout: captured.extend(argv) or SauResult(0, ""))

    result = adapter.upload({
        "platform_id": "baijiahao",
        "account_alias": "creator",
        "media_path": str(media),
        "title": "视频标题",
        "description": "",
        "tags": [],
        "options": {"baijiahao_declaration": "个人观点，仅供参考", "cover_filename": cover.name},
    }, lambda _process: None)

    assert result.returncode == 0
    assert captured[captured.index("--thumbnail") + 1] == str(cover.resolve())
