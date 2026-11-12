"""Persisted authorization and audit wrapper for explicit cloud model calls."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, TypeVar

from ..credentials import CredentialStoreUnavailable
from ..models import StepKind
from ..model_profiles import task_model_settings, task_profile_api_key, transcription_step_settings
from ..providers import ProviderError, UsageUnits, create_provider, get_provider_preset
from ..runner import RunnerError, StepContext, TaskStepSkipped


T = TypeVar("T")
_USER_MESSAGES = {
    "provider_not_configured": "请先在本机设置中保存所选服务商的有效 API Key。",
    "unsupported_capability": "当前配置的模型不支持这个处理步骤。",
    "invalid_input": "提交给模型的材料格式无效。",
    "authentication_failed": "模型服务商 API Key 无效或已失效，请检查本机凭据设置。",
    "rate_limited": "模型服务暂时达到请求限制，请稍后再试。",
    "request_rejected": "模型服务拒绝了本次请求，请检查媒体格式或模型配置。",
    "payload_too_large": "发送给模型的单段材料超过服务限制。",
    "provider_unavailable": "模型服务暂时不可用，请稍后重试。",
    "provider_timeout": "模型请求超时；本次请求结果未知，请先查看调用记录再决定是否重试。",
    "provider_network_error": "无法连接到所选模型服务，请检查网络后再试。",
    "invalid_provider_response": "模型服务返回了无法识别的结果。",
}


def _configured_settings(context: StepContext) -> dict[str, object]:
    if context.app_config is None:
        return {}
    if context.step.kind is StepKind.TRANSCRIBE:
        return transcription_step_settings(context.app_config, context.task.id)
    return task_model_settings(context.app_config, context.task.id)


def configured_model_id(context: StepContext, *, audio: bool = False) -> str:
    if context.app_config is None:
        raise RunnerError("model_not_configured", "模型服务尚未配置。")
    key = "asr_model_id" if audio else "model_id"
    value = _configured_settings(context).get(key)
    if not isinstance(value, str) or not value:
        raise RunnerError("model_not_configured", "请先在本机设置中填写模型 ID。")
    return value


def configured_provider_id(context: StepContext) -> str:
    if context.app_config is None:
        raise RunnerError("model_not_configured", "模型服务尚未配置。")
    value = _configured_settings(context).get("model_provider")
    if not isinstance(value, str) or get_provider_preset(value) is None:
        raise RunnerError("model_not_configured", "请先在本机设置中选择支持的模型服务商。")
    return value


def authorized_model_call(
    context: StepContext,
    *,
    model_id: str,
    input_artifact_ids: list[str],
    consent_artifact_ids: list[str] | None = None,
    operation: Callable[[object], T],
    input_detail: dict[str, object] | None = None,
) -> T:
    """Check task-step consent, commit its exact input IDs, then make one call."""
    if context.cancel_event.is_set():
        raise RunnerError("cancelled", "任务已取消，未发送云端请求。")
    if context.skip_event is not None and context.skip_event.is_set():
        raise TaskStepSkipped
    if not input_artifact_ids or len(set(input_artifact_ids)) != len(input_artifact_ids):
        raise RunnerError("model_input_missing", "模型步骤没有可追溯的本次任务输入。", status_code=422)
    consent_ids = consent_artifact_ids if consent_artifact_ids is not None else input_artifact_ids
    if not consent_ids or len(set(consent_ids)) != len(consent_ids):
        raise RunnerError("model_input_missing", "本次模型调用没有可追溯的授权来源。", status_code=422)

    app_config = context.app_config
    if app_config is None:
        raise RunnerError("model_not_configured", "模型服务尚未配置。")
    config_values = _configured_settings(context)
    provider_id = config_values.get("model_provider")
    preset = get_provider_preset(provider_id)
    if preset is None:
        raise RunnerError("model_not_configured", "请先在本机设置中选择支持的模型服务商。")
    if context.step.kind is StepKind.TRANSCRIBE and preset.transcription_mode == "unsupported":
        raise RunnerError("unsupported_capability", "当前服务商没有接入兼容的语音转写端点。")
    configured_id = (
        config_values.get("asr_model_id")
        if context.step.kind is StepKind.TRANSCRIBE
        else config_values.get("model_id")
    )
    if configured_id != model_id:
        raise RunnerError("model_not_configured", "任务模型与本机设置不一致，请刷新设置后重新创建任务。")

    database = context.repository.database
    with database.transaction(write=True) as connection:
        consent = connection.execute(
            """SELECT id, provider_id, model_id, artifact_ids_json, authorized
               FROM consents WHERE task_id = ? AND step_kind = ?
               ORDER BY authorized_at DESC, rowid DESC LIMIT 1""",
            (context.task.id, context.step.kind.value),
        ).fetchone()
        if (
            consent is None
            or not bool(consent["authorized"])
            or str(consent["provider_id"]) != provider_id
            or str(consent["model_id"]) != model_id
        ):
            raise RunnerError("model_consent_required", "此云端步骤没有本次任务授权，未发送任何材料。")
        try:
            prior_ids = json.loads(consent["artifact_ids_json"])
        except (TypeError, json.JSONDecodeError):
            prior_ids = []
        if not isinstance(prior_ids, list):
            prior_ids = []
        prior_ids = [item for item in prior_ids if isinstance(item, str)]
        if prior_ids and prior_ids != consent_ids:
            raise RunnerError(
                "consent_scope_changed",
                "本次模型输入材料与原授权记录不同；请新建任务并重新确认材料范围。",
            )
        required_ids = list(dict.fromkeys([*consent_ids, *input_artifact_ids]))
        placeholders = ",".join("?" for _ in required_ids)
        rows = connection.execute(
            f"SELECT id FROM artifacts WHERE task_id = ? AND id IN ({placeholders})",
            (context.task.id, *required_ids),
        ).fetchall()
        if {str(row["id"]) for row in rows} != set(required_ids):
            raise RunnerError("model_input_missing", "模型输入材料不属于本次任务，未发送任何材料。", status_code=422)

        step_row = connection.execute(
            "SELECT input_artifact_ids_json, status FROM task_steps WHERE id = ? AND task_id = ?",
            (context.step.id, context.task.id),
        ).fetchone()
        if step_row is None or step_row["status"] != "running":
            raise RunnerError("step_state_changed", "任务步骤状态已变化，未发送任何材料。")
        connection.execute(
            "UPDATE consents SET artifact_ids_json = ? WHERE id = ? AND task_id = ? AND step_kind = ?",
            (json.dumps(consent_ids, separators=(",", ":")), str(consent["id"]), context.task.id, context.step.kind.value),
        )
        connection.execute(
            "UPDATE task_steps SET input_artifact_ids_json = ? WHERE task_id = ? AND id = ?",
            (json.dumps(consent_ids if context.step.kind is StepKind.ANALYZE_STRUCTURE else input_artifact_ids, separators=(",", ":")), context.task.id, context.step.id),
        )
        run_id = uuid.uuid4().hex
        detail = dict(input_detail or {})
        if context.step.kind is not StepKind.TRANSCRIBE:
            detail["client_request_id"] = run_id
        usage_json = {
            "input_tokens": None,
            "audio_seconds": None,
            "image_count": None,
            "output_tokens": None,
            "input_detail": detail,
        }
        now = _utc_now_iso()
        connection.execute(
            """INSERT INTO model_runs (
                   id, task_id, step_id, provider_id, model_id, artifact_ids_json,
                   request_id, usage_json, cost_json, status, error_code, created_at, finished_at
               ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'running', NULL, ?, NULL)""",
            (
                run_id,
                context.task.id,
                context.step.id,
                provider_id,
                model_id,
                json.dumps(input_artifact_ids, separators=(",", ":")),
                json.dumps(usage_json, ensure_ascii=False, separators=(",", ":")),
                json.dumps({"currency": "CNY", "amount": None, "known": False}, separators=(",", ":")),
                now,
            ),
        )

    try:
        api_key = task_profile_api_key(config_values)
        if not api_key:
            raise RunnerError("provider_not_configured", _USER_MESSAGES["provider_not_configured"])
        base_url = config_values.get("model_base_url")
        provider = create_provider(
            api_key,
            base_url=base_url if isinstance(base_url, str) else None,
            provider_id=str(provider_id),
        )
        if context.step.kind is not StepKind.TRANSCRIBE:
            provider.set_client_request_id(run_id)
        if context.cancel_event.is_set():
            raise RunnerError("cancelled", "任务已取消，未发送云端请求。")
        if context.skip_event is not None and context.skip_event.is_set():
            raise TaskStepSkipped
        response = operation(provider)
        if context.cancel_event.is_set():
            was_cancelled = True
        else:
            was_cancelled = False
        was_skipped = context.skip_event is not None and context.skip_event.is_set()
        request_id = getattr(response, "request_id", None)
        usage = getattr(response, "usage", UsageUnits())
        usage_payload = _usage_payload(usage, detail)
        _finish_run(database, run_id, status="completed", request_id=request_id, usage=usage_payload)
        if was_cancelled:
            raise RunnerError("cancelled", "模型已返回结果，但任务随后被取消。")
        if was_skipped:
            raise TaskStepSkipped
        return response
    except TaskStepSkipped:
        _finish_run(database, run_id, status="cancelled", error_code="step_skipped")
        raise
    except RunnerError as exc:
        _finish_run(database, run_id, status="failed", error_code=exc.code)
        if context.skip_event is not None and context.skip_event.is_set() and not context.cancel_event.is_set():
            raise TaskStepSkipped from None
        raise
    except CredentialStoreUnavailable:
        code = "provider_not_configured"
        _finish_run(database, run_id, status="failed", error_code=code)
        raise RunnerError(code, _USER_MESSAGES[code]) from None
    except ProviderError as exc:
        code = getattr(exc, "code", "provider_error")
        _finish_run(database, run_id, status="failed", error_code=code)
        if context.skip_event is not None and context.skip_event.is_set() and not context.cancel_event.is_set():
            raise TaskStepSkipped from None
        raise RunnerError(code, _USER_MESSAGES.get(code, "模型调用失败。")) from None
    except Exception:
        _finish_run(database, run_id, status="failed", error_code="model_call_failed")
        if context.skip_event is not None and context.skip_event.is_set() and not context.cancel_event.is_set():
            raise TaskStepSkipped from None
        raise RunnerError("model_call_failed", "模型调用失败；请查看本地调用记录后决定是否重试。") from None


def _usage_payload(usage: object, detail: dict[str, object]) -> dict[str, object]:
    raw = asdict(usage) if hasattr(usage, "__dataclass_fields__") else {}
    result: dict[str, object] = {"input_detail": detail}
    for key in ("input_tokens", "audio_seconds", "image_count", "output_tokens"):
        value = raw.get(key)
        if isinstance(value, Decimal):
            value = str(value)
        result[key] = value
    return result


def _finish_run(
    database,
    run_id: str,
    *,
    status: str,
    request_id: object = None,
    usage: dict[str, object] | None = None,
    error_code: str | None = None,
) -> None:
    with database.transaction(write=True) as connection:
        connection.execute(
            """UPDATE model_runs SET request_id = ?, usage_json = COALESCE(?, usage_json), status = ?,
                   error_code = ?, finished_at = ? WHERE id = ? AND status = 'running'""",
            (
                request_id if isinstance(request_id, str) else None,
                json.dumps(usage, ensure_ascii=False, separators=(",", ":")) if usage is not None else None,
                status,
                error_code,
                _utc_now_iso(),
                run_id,
            ),
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = ["authorized_model_call", "configured_model_id", "configured_provider_id"]
