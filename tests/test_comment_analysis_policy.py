from datetime import datetime, timezone
import json

from app.workbench.comment_analysis_policy import batch_input, choose_groups, comment_snapshot, group_comments, partition_groups
from app.workbench.models import CommentRecord


def comment(index, text, likes=0):
    return CommentRecord(id=str(index), platform_comment_id='platform-' + str(index), parent_id=None, author=None, text=text, like_count=likes, created_at=None, collected_at=datetime.now(timezone.utc))


def test_exact_dedup_preserves_distinct_comments_and_low_like_questions():
    groups = group_comments([comment(1, ' 为什么？ ', 0), comment(2, '为什么？', 8), comment(3, '为什么!', 0)])
    assert len(groups) == 2
    assert len(groups[0].comments) == 2
    assert '提问线索' in groups[0].signals
    assert groups[0] in choose_groups(groups, 'economy', 2)


def test_economy_fills_budget_when_only_ordinary_comments_exist():
    groups = group_comments([comment(index, '好的' + str(index)) for index in range(20)])
    assert len(choose_groups(groups, 'economy', 10)) == 10
    assert len(choose_groups(groups, 'full', 1)) == 20


def test_economy_reserves_space_for_specific_low_like_questions():
    comments = [comment(index, '为什么高赞问题' + str(index) + '？', 500) for index in range(20)]
    comments += [comment(100 + index, '为什么低赞问题' + str(index) + '？', 0) for index in range(20)]
    result = choose_groups(group_comments(comments), 'economy', 10)
    assert len(result) == 10
    assert sum(group.comments[0].like_count == 0 for group in result) >= 3


def test_batch_uses_short_ordinals_and_respects_count_and_byte_limits():
    groups = group_comments([comment(index, 'a' * 20 + str(index)) for index in range(5)])
    batches = partition_groups(groups, 3, 100)
    assert sum(map(len, batches)) == 5
    assert all(len(batch) <= 3 for batch in batches)
    assert all(len(batch_input(batch).encode('utf-8')) <= 100 for batch in batches)
    assert all('platform-' not in batch_input(batch) for batch in batches)
    assert json.loads(batch_input(batches[0]))[0]['n'] == 1


def test_preview_snapshot_is_stable_but_changes_when_comments_change():
    first, second = comment(1, '问题？', 1), comment(2, '建议', 0)
    assert comment_snapshot([first, second]) == comment_snapshot([second, first])
    assert comment_snapshot([first, second]) != comment_snapshot([comment(1, '问题？', 2), second])
