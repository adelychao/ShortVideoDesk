from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.workbench import archive, runner
from app.workbench.archive import archive_path
from app.workbench.steps.import_media import _validate_publication_manifest


def test_archive_number_continues_after_legacy_folders_and_removes_tags(tmp_path):
    day = tmp_path / '2026-10-29'
    day.mkdir()
    (day / '0001_旧标题_douyin_123').mkdir()
    (day / '0003_另一标题_douyin_456').mkdir()
    (day / '9999_普通文件.txt').write_text('not a folder', encoding='utf-8')
    result = archive_path(tmp_path, datetime(2026, 10, 29), '标题内容 #职场 #认知', 'douyin', '123')
    assert result.name == '0004_标题内容'
    assert archive_path(tmp_path, datetime(2026, 10, 29), '第二标题#职场', 'douyin', '456').name == '0005_第二标题'
    assert archive_path(tmp_path, datetime(2026, 10, 30), 'C#语言入门 #编程', 'local', '123').name == '0001_C#语言入门'


def test_concurrent_archive_creations_do_not_repeat_sequence(tmp_path):
    def create(index):
        return archive_path(tmp_path, datetime(2026, 10, 29), f'标题{index} #标签', 'douyin', str(index)).name

    with ThreadPoolExecutor(max_workers=12) as pool:
        names = list(pool.map(create, range(40)))
    assert sorted(int(name.split('_', 1)[0]) for name in names) == list(range(1, 41))
    assert all('#' not in name for name in names)


def test_existing_file_with_target_name_is_not_overwritten(tmp_path):
    day = tmp_path / '2026-10-29'
    day.mkdir()
    occupied = day / '0001_标题'
    occupied.write_text('keep', encoding='utf-8')
    assert archive_path(tmp_path, datetime(2026, 10, 29), '标题', 'local', '1').name == '0002_标题'
    assert occupied.read_text(encoding='utf-8') == 'keep'


@pytest.mark.parametrize('title', [
    '什么样的人天生适合当领导_ #职场',
    '什么样的人天生适合当领导？ #职场',
    '什么样的人天生适合当领导___#职场',
])
def test_directory_removes_trailing_underscores_left_by_title_sanitization(tmp_path, title):
    result = archive_path(tmp_path, datetime(2026, 10, 30), title, 'douyin', '1')
    assert result.name == '0001_什么样的人天生适合当领导'


def test_directory_preserves_internal_underscore_and_handles_empty_title(tmp_path):
    assert archive_path(tmp_path, datetime(2026, 10, 30), '职场_A_B_ #话题', 'douyin', '1').name == '0001_职场_A_B'
    assert archive_path(tmp_path, datetime(2026, 10, 30), '___ #话题', 'douyin', '2').name == '0002_未命名素材'


def test_utc_timestamp_uses_local_date_for_directory(tmp_path, monkeypatch):
    china = timezone(timedelta(hours=8))
    monkeypatch.setattr(archive, 'local_archive_time', lambda value: value.astimezone(china), raising=False)
    utc_time = datetime(2026, 10, 29, 22, 40, tzinfo=timezone.utc)
    result = archive_path(tmp_path, utc_time, '标题', 'douyin', '1')
    assert result.parent.name == '2026-10-30'


def test_download_manifest_preserves_local_offset_for_recovery(tmp_path, monkeypatch):
    china = timezone(timedelta(hours=8))
    monkeypatch.setattr(runner, 'local_archive_time', lambda value: value.astimezone(china), raising=False)
    folder = tmp_path / '2026-10-30' / '0001_标题'
    folder.mkdir(parents=True)
    utc_time = datetime(2026, 10, 29, 22, 40, tzinfo=timezone.utc)
    manifest = runner._download_publication_manifest('a', 't', 's', 'x', utc_time, folder, tmp_path, [], cleanup_staging=[])
    assert manifest['archived_at'] == '2026-10-30T06:40:00+08:00'


@pytest.mark.parametrize(('day', 'folder_name', 'archived_at'), [
    ('2026-10-29', '0001_新标题', '2026-10-29T08:00:00+00:00'),
    ('2026-10-29', '0001_旧标题_local_123', '2026-10-29T08:00:00+00:00'),
    ('2026-10-30', '0001_本地日期', '2026-10-30T06:40:00+08:00'),
])
def test_import_recovery_accepts_new_and_existing_directory_names(tmp_path, day, folder_name, archived_at):
    root = tmp_path / 'archive'
    folder = root / day / folder_name
    folder.mkdir(parents=True)
    publication_id = 'a' * 32
    marker = folder / f'.workbench-import-{publication_id}.manifest.json'
    value = {
        'version': 1, 'id': publication_id, 'task_id': 't', 'step_id': 's',
        'asset_id': 'a', 'artifact_id': 'r', 'title': '标题', 'artifact_version': 1,
        'size_bytes': 10, 'sha256': 'b' * 64, 'extension': '.mp4', 'media_type': 'video',
        'metadata': {'media_type': 'video', 'size_bytes': 10, 'source_extension': '.mp4'},
        'archived_at': archived_at,
        'archive_directory': f'{day}/{folder_name}',
        'target': f'{day}/{folder_name}/media.mp4',
        'staged': f'.workbench-import-{publication_id}.tmp',
    }
    _, target, extension = _validate_publication_manifest(value, marker, root, publication_id)
    assert target == folder / 'media.mp4'
    assert extension == '.mp4'
