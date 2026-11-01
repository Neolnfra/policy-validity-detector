"""
api/webhook.py —— 企业微信 / 飞书机器人及外部知识库回调
================================================================================

场景价值
--------
政策过期检测器真正落地的形态，不是"一个网页"，而是"嵌进业务流程的一个动作"。
例如：招商同事在企业微信群里贴一份政策名，机器人立刻回一条"该文件已于 2023-12-31 失效，
现行口径见 XXX"。本模块就是这条通路的入口。

安全约定
--------
- 若 .env 配置了 WEBHOOK_TOKEN，则必须携带一致的 token，否则拒绝；
- 未配置 token 时仅允许本机/内网演示使用，返回体里会明确提示"未启用校验"。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from api.schemas import WebhookPayload, WebhookResponse
from config import settings
from core.region_router import detect_region
from core.yuanbao_client import get_mock_index as _get_mock_index

router = APIRouter(prefix="/api/v1/webhook", tags=["机器人回调"])

_STATUS_TAG = {"VALID": "现行有效", "REPLACED": "部分失效", "EXPIRED": "已废止", "UNCERTAIN": "存疑"}


def _verify_token(token: str | None) -> None:
    if settings.webhook_token and token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="webhook token 校验失败")


def _extract_policy_text(payload: WebhookPayload) -> str:
    for candidate in (payload.policy, payload.text, payload.content):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _render_reply(result: Dict[str, Any]) -> str:
    """把核验结果压缩成一条适合群聊阅读的卡片文本。"""
    meta = result.get("policy_meta", {})
    validity = result.get("validity_result", {})
    tag = _STATUS_TAG.get(validity.get("status_code", "UNCERTAIN"), "存疑")
    display_title = (
        meta.get("title")
        or meta.get("locked_policy_title")
        or (f"「{meta.get('topic')}」相关政策" if meta.get("topic") else "—")
    )

    lines = [
        f"**【{tag}】** {validity.get('status_label', '存疑')}　"
        f"可信度 {validity.get('confidence_score', 0)} / 权威度 {validity.get('authority_score', 0)}",
        "",
        f"**政策**：{display_title}",
    ]
    if meta.get("doc_number"):
        lines.append(f"**文号**：{meta['doc_number']}")
    lines.append(f"**结论**：{validity.get('summary', '')}")

    sources = result.get("sources", [])
    if sources:
        lines.append("")
        lines.append("**首要信源**：")
        for source in sources[:2]:
            lines.append(f"- [{source.get('title', '')[:40]}]({source.get('url', '')})")

    mode = result.get("mode", "mock_fallback")
    if mode != "real_api":
        lines.append("")
        lines.append("> 当前为演示模式，结论来自内置案例库，请以政务网原文为准。")
    return "\n".join(lines)


async def _handle(payload: WebhookPayload) -> Dict[str, Any]:
    _verify_token(payload.token)

    text = _extract_policy_text(payload)
    if not text:
        raise HTTPException(status_code=422, detail="请提供 policy / text / content 之一作为待核验政策文本")

    # 延迟导入，避免模块级循环依赖
    from api.routes import run_verification
    from api.schemas import VerifyRequest

    region_match = detect_region(text, "auto")
    result = await run_verification(
        VerifyRequest(raw_input=text, region_hint=region_match.region.key)
    )
    return result


@router.post("/wecom", response_model=WebhookResponse, summary="企业微信群机器人回调")
async def wecom_webhook(payload: WebhookPayload) -> Dict[str, Any]:
    result = await _handle(payload)
    content = _render_reply(result)
    if settings.webhook_token:
        suffix = ""
    else:
        suffix = "\n\n> 提示：未配置 WEBHOOK_TOKEN，当前仅建议内网演示使用。"
    return {
        "code": 200,
        "msgtype": "markdown",
        "markdown": {"content": content + suffix},
        "content": content + suffix,
    }


@router.post("/feishu", response_model=WebhookResponse, summary="飞书自定义机器人回调")
async def feishu_webhook(payload: WebhookPayload) -> Dict[str, Any]:
    result = await _handle(payload)
    content = _render_reply(result)
    return {
        "code": 200,
        "msg_type": "text",
        "content": {"text": content.replace("**", "")},
        "markdown": content,
    }


@router.get("/health", summary="回调通道自检")
async def webhook_health() -> Dict[str, Any]:
    return {
        "code": 200,
        "token_enforced": bool(settings.webhook_token),
        "status": "ready",
        "mock_cases": len(_get_mock_index().cases),
    }
