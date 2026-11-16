"""本地评论预处理与经济分析策略。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Sequence

from .models import CommentRecord


@dataclass(frozen=True)
class CommentGroup:
    text: str
    comments: tuple[CommentRecord, ...]
    signals: tuple[str, ...]
    fingerprint: str


def _signals(text: str) -> tuple[str, ...]:
    signals: list[str] = []
    if re.search(r"[?？]|(?:为什么|怎么|如何|能否|请问|可不可以|怎么办|有没有|吗[。！!\s]*$)", text):
        signals.append("提问线索")
    if re.search(r"(?:下一期|下期|出一期|想看|能讲|能不能讲|想听|希望讲|能做一期)", text):
        signals.append("选题线索")
    if re.search(r"(?:建议|配乐|背景音乐|声音太|听不清|字幕|讲错|不准确|有误|纠正)", text):
        signals.append("反馈线索")
    if len(text) >= 24:
        signals.append("具体内容")
    return tuple(signals)


def group_comments(comments: Sequence[CommentRecord]) -> list[CommentGroup]:
    """只合并去除首尾空白后完全相同的正文，保留每条原评论。"""
    groups: dict[str, list[CommentRecord]] = {}
    for comment in comments:
        groups.setdefault(comment.text.strip(), []).append(comment)
    return [
        CommentGroup(text, tuple(items), _signals(text), hashlib.sha256(text.encode('utf-8')).hexdigest())
        for text, items in groups.items()
    ]


def comment_snapshot(comments: Sequence[CommentRecord]) -> str:
    digest = hashlib.sha256()
    for comment in sorted(comments, key=lambda item: item.platform_comment_id):
        row = [comment.platform_comment_id, comment.text, comment.like_count]
        digest.update(json.dumps(row, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def choose_groups(groups: Sequence[CommentGroup], mode: str, candidate_limit: int) -> list[CommentGroup]:
    if mode not in {'economy', 'full'}:
        raise ValueError('invalid comment analysis mode')
    if candidate_limit < 1:
        raise ValueError('candidate_limit must be positive')
    nonempty = [group for group in groups if group.text]
    if mode == 'full':
        return nonempty
    # 作者：zhw，时间：2026-11-14 14:55:14。低赞具体问题同样优先；规则只是候选线索。
    priority = [group for group in nonempty if group.signals]
    ordinary = [group for group in nonempty if not group.signals]
    priority.sort(key=lambda group: (-len(group.signals), -max((item.like_count or 0) for item in group.comments), group.fingerprint))
    ordinary.sort(key=lambda group: group.fingerprint)
    reserve_ordinary = min(len(ordinary), max(1, candidate_limit // 10)) if ordinary else 0
    low_like = [group for group in priority if max((item.like_count or 0) for item in group.comments) <= 2]
    reserve_low = min(len(low_like), candidate_limit * 3 // 10, candidate_limit - reserve_ordinary)
    low_keys = {group.fingerprint for group in low_like}
    other_priority = [group for group in priority if group.fingerprint not in low_keys]
    selected = other_priority[:candidate_limit - reserve_ordinary - reserve_low]
    selected.extend(low_like[:reserve_low])
    selected.extend(ordinary[:reserve_ordinary])
    chosen = {group.fingerprint for group in selected}
    if len(selected) < candidate_limit:
        remaining = [group for group in priority if group.fingerprint not in chosen]
        selected.extend(remaining[:candidate_limit - len(selected)])
    if len(selected) < candidate_limit:
        selected.extend(ordinary[reserve_ordinary:reserve_ordinary + candidate_limit - len(selected)])
    return selected[:candidate_limit]


def partition_groups(groups: Sequence[CommentGroup], batch_size: int, byte_limit: int) -> list[list[CommentGroup]]:
    if not 1 <= batch_size <= 500 or byte_limit < 1:
        raise ValueError('invalid batch limits')
    batches: list[list[CommentGroup]] = []
    current: list[CommentGroup] = []
    for group in groups:
        candidate = [*current, group]
        encoded = json.dumps([{'n': n, 'text': item.text} for n, item in enumerate(candidate, 1)], ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if current and (len(candidate) > batch_size or len(encoded) > byte_limit):
            batches.append(current)
            current = [group]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def batch_input(batch: Sequence[CommentGroup]) -> str:
    return json.dumps([{'n': index, 'text': group.text} for index, group in enumerate(batch, 1)], ensure_ascii=False, separators=(',', ':'))


def save_options(database, task_id: str, step_id: str, options: dict[str, object], created_at: str) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            'INSERT INTO comment_analysis_options (step_id, task_id, options_json, created_at) VALUES (?, ?, ?, ?)',
            (step_id, task_id, json.dumps(options, ensure_ascii=False, sort_keys=True, separators=(',', ':')), created_at),
        )


def load_options(database, task_id: str, step_id: str) -> dict[str, object] | None:
    with database.transaction() as connection:
        row = connection.execute(
            'SELECT options_json FROM comment_analysis_options WHERE task_id = ? AND step_id = ?',
            (task_id, step_id),
        ).fetchone()
    if row is None:
        return None
    value = json.loads(row['options_json'])
    if not isinstance(value, dict):
        raise ValueError('invalid comment analysis options')
    return value
