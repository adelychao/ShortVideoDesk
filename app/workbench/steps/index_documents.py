"""Task step for indexing the selected local archive's registered text."""

from __future__ import annotations

from ..models import StepStatus
from ..runner import StepContext, StepResult, TaskCancelled
from ..search import refresh_local_index


def index_documents(context: StepContext) -> StepResult:
    if context.cancel_event.is_set():
        raise TaskCancelled
    context.progress(context.step.id, None, "正在检查本地文本并更新 SQLite 全文索引")
    result = refresh_local_index(context.repository.database, context.archive_root)
    if context.cancel_event.is_set():
        raise TaskCancelled
    context.progress(context.step.id, 100, f"本地索引已更新，当前登记 {result['documents']} 篇文本")
    return StepResult(
        status=StepStatus.COMPLETED.value,
        metadata={"result": {"index": result, "scope": "已登记的本地文本产物与素材标题"}},
    )
