import pytest

from app.workbench.database import Database
from app.workbench.video_publishing import SauResult, SocialAutoUploadAdapter


@pytest.mark.parametrize('platform', ['xiaohongshu', 'kuaishou', 'bilibili', 'baijiahao'])
def test_dispatch_sends_schedule_to_platform_uploader_without_waiting(tmp_path, monkeypatch, platform):
    adapter = SocialAutoUploadAdapter(Database(tmp_path / 'db.sqlite'))
    sent = []
    monkeypatch.setattr(adapter, 'run', lambda args, on_start, timeout: sent.append(args) or SauResult(0, ''))
    target = {'platform_id': platform, 'account_alias': 'creator', 'media_path': str(tmp_path / 'video.mp4'),
              'title': 'title', 'description': 'description', 'tags': [],
              'schedule_at': '2026-11-04 00:50',
              'options': {'bilibili': {'tid': 122}, 'baijiahao': {'baijiahao_declaration': '个人观点，仅供参考'}}.get(platform, {})}
    result = adapter.upload(target, lambda process: None)
    assert result.returncode == 0
    assert sent[0][0:2] == [platform, 'upload-video']
    assert sent[0][sent[0].index('--schedule') + 1] == '2026-11-04 00:50'
    if platform == 'baijiahao':
        assert sent[0][sent[0].index('--declaration') + 1] == '个人观点，仅供参考'
        target['options'] = {}
        assert '选择符合实际的创作声明' in adapter.upload(target, lambda process: None).launch_error
        assert len(sent) == 1
