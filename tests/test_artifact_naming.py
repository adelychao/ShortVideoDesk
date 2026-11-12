import hashlib
import threading
from pathlib import Path

from app.workbench.config import AppConfig
from app.workbench.database import Database
from app.workbench.models import StepKind
from app.workbench.runner import StepContext, TaskRunner
from app.workbench.steps.artifact_store import register_bytes
from app.workbench.steps.comments import _choose_note_target


def test_generated_markdown_uses_readable_safe_chinese_names(tmp_path):
    db = Database(tmp_path / 'data.sqlite')
    config = AppConfig(db)
    root = config.set_archive_root(tmp_path / 'archive')
    runner = TaskRunner(db, config)
    try:
        _, task = runner.create_download_task('https://www.douyin.com/video/123', step_kinds=[StepKind.DOWNLOAD])
        step = task.steps[0]
        folder = root / 'video'; folder.mkdir()
        media = folder / 'video.mp4'; media.write_bytes(b'video')
        with db.transaction(write=True) as c:
            c.execute("UPDATE task_steps SET status='running' WHERE id=?", (step.id,))
            c.execute("INSERT INTO artifacts (id,task_id,step_id,kind,path,sha256,version,metadata_json,created_at) VALUES ('media',?,?,'video',?,?,1,'{}','now')", (task.id,step.id,str(media),hashlib.sha256(b'video').hexdigest()))
        context = StepContext(task, step, runner.repository, root, threading.Event(), lambda *_: None, '')
        transcript = register_bytes(context, 'transcript', '.md', b'text', {})
        ocr = register_bytes(context, 'ocr_result', '.md', b'text', {})
        script = register_bytes(context, 'script', '.md', b'text', {'style':'故事口语'})
        unsafe = register_bytes(context, 'script', '.md', b'text', {'style':'../../escape'})
        assert Path(transcript.path).name.startswith('语音转写-')
        assert Path(ocr.path).name.startswith('画面文字OCR-')
        assert Path(script.path).name.startswith('口播稿-故事口语-')
        assert Path(unsafe.path).parent == folder
        assert '..' not in Path(unsafe.path).name
        assert len({transcript.path, ocr.path, script.path, unsafe.path}) == 4
    finally:
        runner.stop()


def test_comment_material_name_does_not_claim_to_be_full_video_notes(tmp_path):
    first = _choose_note_target(tmp_path, '1234567890')
    assert first.name == '评论资料.md'
    first.write_text('existing', encoding='utf-8')
    assert _choose_note_target(tmp_path, '1234567890').name.startswith('评论资料-')
    assert first.read_text(encoding='utf-8') == 'existing'
