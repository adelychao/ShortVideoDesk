from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.workbench import api, article_platforms, video_publishing
from app.workbench.article_studio import create_article, default_document, save_article
from app.workbench.config import AppConfig, IMAGE_DIRECTORY_DEFAULTS
from app.workbench.database import Database
from app.workbench.runner import TaskRunner
from app.workbench.uploads import UploadReceipt


def png(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (160, 120), color).save(buffer, format="PNG")
    return buffer.getvalue()


def image_api(tmp_path):
    database = Database(tmp_path / "workbench.sqlite3")
    config = AppConfig(database)
    database.migrate()
    app = FastAPI()
    app.state.workbench_runner = TaskRunner(database, config)
    app.include_router(api._router)
    return TestClient(app), config


def test_article_publish_cover_is_scoped_without_changing_finalized_article(tmp_path):
    client, config = image_api(tmp_path)
    config.update_settings({"article_image_root": str(tmp_path / "covers")})
    document = default_document("封面测试")
    document["body_markdown"] = "正文"
    article = create_article(config.database, document)
    from app.workbench.article_studio import finalize_article
    finalized = finalize_article(config.database, article["id"])
    other = create_article(config.database, document)
    data = png("blue")
    response = client.post(f'/api/v1/articles/{article["id"]}/publish-covers', files={"file": ("cover.png", data)})
    assert response.status_code == 201, response.text
    cover = response.json()
    assert client.get(cover["preview_url"]).content == data
    assert client.get(f'/api/v1/articles/{other["id"]}/publish-covers/{cover["id"]}').status_code == 404
    current = client.get(f'/api/v1/articles/{article["id"]}').json()
    assert current["version"] == finalized["version"]
    assert current["status"] == "finalized"


def test_publish_cover_options_are_frozen_for_the_target(tmp_path):
    database = Database(tmp_path / "covers.sqlite3")
    database.migrate()
    document = default_document("封面测试")
    document["body_markdown"] = "正文"
    reference = f'data/article-assets/{"a" * 32}/{"b" * 32}.png'
    result = api._article_platform_document("a" * 32, document, "wechat", {"cover_image_path": reference})
    assert result["cover_image_path"] == reference
    assert document["cover_image_path"] == ""


def test_article_publish_cover_rejects_corrupt_image_with_a_valid_header(tmp_path):
    client, config = image_api(tmp_path)
    document = default_document("封面测试")
    document["body_markdown"] = "正文"
    article = create_article(config.database, document)
    corrupt = b"\x89PNG\r\n\x1a\n" + b"not-an-image"
    response = client.post(f'/api/v1/articles/{article["id"]}/publish-covers', files={"file": ("cover.png", corrupt)})
    assert response.status_code == 415


def test_article_image_settings_keep_existing_images_and_reuse_identical_content(tmp_path, monkeypatch):
    client, config = image_api(tmp_path)
    settings = client.get("/api/v1/config").json()["settings"]
    for key, path in IMAGE_DIRECTORY_DEFAULTS.items():
        assert settings[key] == str(path)
    first, second, cover_root = (tmp_path / name for name in ("first", "second", "covers"))
    settings.update(article_image_root=str(first), video_publish_cover_root=str(cover_root))
    assert client.put("/api/v1/config", json=settings).status_code == 200
    draft_document = default_document("Storage")
    draft_document["blocks"] = [{"type": "paragraph", "text": "A paragraph with an illustration."}]
    article = create_article(config.database, draft_document, allow_empty_body=True)
    article_url = f'/api/v1/articles/{article["id"]}'
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    legacy_reference = f'data/article-assets/{article["id"]}/{"b" * 32}.png'
    legacy_path = tmp_path / legacy_reference
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(png("green"))
    save_article(config.database, article["id"], article["version"], {
        **article["document"], "cover_image_path": legacy_reference,
    })
    assert client.get(article_url + "/cover").content == legacy_path.read_bytes()

    original = png("red")
    assert client.post(article_url + "/cover", files={"file": ("original.png", original)}).status_code == 201
    duplicate = client.post(article_url + "/cover", files={"file": ("identical.png", original)})
    assert duplicate.status_code == 201
    previous_name = duplicate.json()["article"]["document"]["cover_image_path"].rsplit("/", 1)[-1]
    assert len(list(first.rglob("*.png"))) == 1
    config.update_settings({"article_image_root": str(second)})
    assert client.get(article_url + "/cover").content == original
    assert config.get_settings()["video_publish_cover_root"] == str(cover_root)

    generated = png("blue")
    monkeypatch.setattr(api, "generate_vector_illustration", lambda *a, **k: (
        generated, {"model_id": "test", "generated_at": "2026-11-09", "request_id": ""},
    ))
    response = client.post(article_url + "/images/generate", json={"target": "cover", "confirmed": True, "mode": "vector"})
    assert response.status_code == 200, response.text
    assert client.get(response.json()["image_url"]).content == generated
    assert client.get(article_url + "/images/" + previous_name).content == original
    block_id = response.json()["article"]["document"]["blocks"][0]["id"]
    paragraph = client.post(article_url + "/images/generate", json={
        "target": "paragraph", "block_id": block_id, "confirmed": True, "mode": "vector",
    })
    assert paragraph.status_code == 200, paragraph.text
    assert paragraph.json()["image_url"] == response.json()["image_url"]
    assert len(list(second.rglob("*.png"))) == 1
    document = response.json()["article"]["document"]
    assert article_platforms.xiaohongshu_article_images(article["id"], document, database=config.database)[0].parent.parent == second
    assert client.delete(article_url).status_code == 200
    assert not list(first.rglob("*.png"))
    assert not list(second.rglob("*.png"))
    assert not legacy_path.exists()


def test_video_cover_survives_draft_restore_directory_change_and_shared_cleanup(tmp_path, monkeypatch):
    client, config = image_api(tmp_path)
    database = config.database
    video = video_publishing
    monkeypatch.setattr(video, "PROJECT_ROOT", tmp_path)
    for name, directory in (
        ("_PUBLISH_MEDIA_ROOT", "batches"), ("_PUBLISH_COVER_UPLOAD_ROOT", "legacy"),
        ("_VIDEO_PUBLISH_DRAFT_ROOT", "drafts"),
    ):
        monkeypatch.setattr(video, name, tmp_path / directory)
    first, second = tmp_path / "covers-a", tmp_path / "covers-b"
    config.update_settings({"video_publish_cover_root": str(first)})
    content = png("red")
    response = client.post("/api/v1/video-publish/covers", files={"file": ("cover.png", content)})
    assert response.status_code == 201, response.text
    cover = response.json()
    assert client.get(cover["preview_url"]).content == content
    cover_id = cover["upload_id"]
    assert list(first.glob("*.png")) == [first / f"{cover_id}.png"]

    media_path = tmp_path / "original.mp4"
    media = b"fixture for a validated video receipt"
    media_path.write_bytes(media)
    receipt = UploadReceipt("a" * 32, media_path.name, len(media), sha256(media).hexdigest(), "video", False, media_path)
    monkeypatch.setattr(video, "get_upload", lambda *a, **k: receipt)
    monkeypatch.setattr(video, "delete_upload", lambda *a: True)

    async def restore_media(*args, **kwargs):
        return receipt

    monkeypatch.setattr(video, "save_upload_from_path", restore_media)
    draft = video.save_video_publish_draft(
        database, draft_id=None, upload_id=receipt.upload_id,
        cover_upload_ids={"common:portrait": cover_id}, form={"title": "Original"},
    )
    config.update_settings({"video_publish_cover_root": str(second)})
    restored = client.post(f'/api/v1/video-publish/drafts/{draft["id"]}/restore')
    assert restored.status_code == 200, restored.text
    assert restored.json()["cover_uploads"]["common:portrait"]["upload_id"] == cover_id
    assert not list((tmp_path / "drafts").rglob("*.png"))
    assert not list(second.glob("*.png"))

    with database.transaction(write=True) as connection:
        connection.execute(
            """INSERT INTO video_publish_accounts
               (id, platform_id, account_alias, status, created_at, updated_at)
               VALUES ('account', 'kuaishou', 'creator', 'connected', '2026-11-09', '2026-11-09')""",
        )
    batch = video.create_video_publish_batch(
        database, archive_root=None, idempotency_key="cover-sharing-test", asset_id=None, upload_id=receipt.upload_id,
        title="Original", description="", tags=[], schedule_at=None,
        targets=[{"account_id": "account", "options": {"cover_upload_id": cover_id}}],
    )
    filename = batch["targets"][0]["options"]["cover_filename"]
    path = video.get_video_publish_batch_cover_path(database, batch["id"], filename)
    assert path == first / f"{cover_id}.png"
    assert not list((tmp_path / "batches").rglob("*.png"))
    claimed = video._claim_video_publish_target(database, batch["targets"][0]["id"])
    assert claimed["managed_cover_paths"][filename] == path

    video.delete_video_publish_draft(database, draft["id"])
    cutoff = datetime.now(timezone.utc) + timedelta(days=2)
    video.cleanup_expired_video_publish_media(database, now=cutoff)
    assert path.is_file()
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE video_publish_targets SET status = 'published'")
    video.clean_video_publish_batch_media(database, batch["id"])
    video.cleanup_expired_video_publish_media(database, now=cutoff)
    assert not path.exists()
    assert media_path.is_file()
