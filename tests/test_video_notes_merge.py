from datetime import datetime, timezone

from app.workbench.video_notes import render_video_notes


def test_video_notes_merge_archive_metadata_and_comment_analysis():
    content, _ = render_video_notes(
        title='标题', platform='douyin', source_id='123', source_url='https://example.com',
        author='作者', published_at=None, archived_at=None,
        transcript_markdown='## 校对文案\n正文', transcript_artifact_id='transcript',
        source_artifact_ids=['metadata', 'analysis'], comments_markdown=None,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        archive_metadata={'duration_seconds': 410.5, 'width': 1920, 'height': 1080, 'full_decode_verified': True},
        comment_analysis_markdown='# 评论精选与摘要\n\n## 主题\n\n- 测试主题',
    )
    text = content.decode('utf-8')
    assert '410.5' in text and '1920×1080' in text
    assert '媒体完整解码校验：通过' in text
    assert '## 评论精选与摘要' in text and '测试主题' in text


def test_initial_notes_include_metadata_comments_and_analysis_without_transcript():
    from app.workbench.video_notes import render_initial_video_notes
    text = render_initial_video_notes(
        comments_markdown='# 标题 · 视频资料\n\n## 评论\n\n一条评论',
        archive_metadata={'author': '作者甲', 'duration_seconds': 31, 'width': 1920, 'height': 1080},
        comment_analysis_markdown='# 评论精选与摘要\n\n## 主题\n\n- 想法',
    ).decode('utf-8')
    assert '一条评论' in text and '作者甲' in text and '1920×1080' in text and '想法' in text
