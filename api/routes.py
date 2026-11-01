"""
api/routes.py —— 核心 RESTful 接口与 Agent 编排
================================================================================

对外接口一览（全部挂在 /api/v1 下，Swagger 见 /docs）
------------------------------------------------------
    GET  /health                 运行模式与自检信息
    GET  /regions                全国 34 省级行政区 + 重点城市 + 部委 + 演示案例
    POST /policy/parse           仅做要素解析与三路检索词构造（不检索）
    POST /policy/verify          【核心】完整链路：解析 → 三路检索 → 交叉求证 → 判定
    POST /policy/verify/stream   同上，但以 SSE 逐步推送 Agent 思考链
    POST /policy/diff            独立的新旧条款字符级 Diff
    POST /export/report          生成 Markdown 核验报告
    GET  /openapi/snippets       生成 cURL / Python / JS 调用示例（供调试抽屉）

编排原则
--------
`run_verification()` 是唯一的业务真相来源，流式接口只是它的"事件投影"。
两条传输路径共用同一份实现，避免出现"流式结果与非流式结果不一致"这种低级事故。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.schemas import (
    DiffRequest,
    DiffResponse,
    HealthResponse,
    ParseRequest,
    ParseResponse,
    RegionsResponse,
    ReportRequest,
    ReportResponse,
    SettingsConfigRequest,
    SettingsStatusResponse,
    SettingsTestRequest,
    SettingsTestResponse,
    SnippetsResponse,
    VerifyRequest,
    VerifyResponse,
)
from config import settings
from core.query_builder import (
    PolicyMeta,
    build_queries,
    parse_policy_elements,
    resolve_focus_policy,
)
from core.region_router import detect_region, list_regions
from core.runtime_config import (
    Overrides,
    clear_runtime,
    describe_status,
    effective_settings,
    overrides_from_headers,
    set_runtime,
)
from core.validity_judge import (
    build_diff_segments,
    judge_from_case,
    judge_from_evidence,
)
from core.yuanbao_client import ChannelSearchResult, YuanbaoSearchClient, get_mock_index

router = APIRouter(prefix="/api/v1", tags=["政策效力核验"])

EmitFn = Optional[Callable[[Dict[str, Any]], Awaitable[None]]]

# ---------------------------------------------------------------------------
# 轻量缓存：同一输入在 TTL 内不重复打真实接口（省钱、也更快）
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_SECONDS = 600


def _cache_key(req: VerifyRequest, fingerprint: str = "env") -> str:
    """
    缓存键必须带上「凭证指纹」。

    否则 A 用户用自定义 Key 查过的结果会被 B 用户命中，
    既泄露了别人的检索结果，也会让人误以为"换 Key 没生效"。
    """
    return f"{fingerprint}|{req.region_hint}|{req.case_id or ''}|{req.raw_input}"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return entry["value"]


def _cache_put(key: str, value: Dict[str, Any]) -> None:
    if len(_CACHE) > 256:
        _CACHE.clear()
    _CACHE[key] = {"ts": time.time(), "value": value}


async def _emit(emit: EmitFn, payload: Dict[str, Any]) -> None:
    if emit is not None:
        await emit(payload)


def _safe_verification_fallback(payload: VerifyRequest, exc: Exception, eff=None) -> Dict[str, Any]:
    """
    核验链路的最后一道保险。

    设计原则：接口对前端永远是 200 + 结构完整的 JSON。
    任何未预料的异常都在这里收敛成「存疑」结论，而不是 500——
    500 会让前端卡在 Loading，也会在评委演示时直接翻车。
    """
    region_name = ""
    region_key = ""
    gov_domain = ""
    try:
        region_match = detect_region(payload.raw_input, payload.region_hint)
        region_name = region_match.region.name
        region_key = region_match.region.key
        gov_domain = region_match.region.gov_domain
    except Exception:  # noqa: BLE001 —— 兜底路径本身不能再抛异常
        region_name = ""

    reason = f"核验链路出现未预期异常（{type(exc).__name__}），已返回安全兜底结果，未给出任何未经证实的结论。"
    return {
        "code": 200,
        "mode": (eff or settings).search_mode,
        "provider": "fallback",
        "elapsed_ms": 0,
        "cached": False,
        "policy_meta": {
            "title": payload.raw_input[:60],
            "region": region_name,
            "region_key": region_key,
            "gov_domain": gov_domain,
        },
        "region_route": {"region_key": region_key, "region_name": region_name, "gov_domain": gov_domain},
        "validity_result": {
            "status_code": "UNCERTAIN",
            "status_label": "核验服务暂时不可用",
            "status_color": "slate",
            "status_emoji": "",
            "confidence_score": 0,
            "authority_score": 0,
            "summary": reason,
            "hallucination_comparison": {
                "naive_llm_answer": "",
                "yuanbao_verified_answer": reason,
            },
        },
        "search_trace": [],
        "timeline": [],
        "clause_diffs": [],
        "sources": [],
        "queries": [],
        "notes": [reason],
        "matched_case_id": None,
        "judge_source": "fallback",
        "compliance_guide": None,
    }


# ---------------------------------------------------------------------------
# 核心编排
# ---------------------------------------------------------------------------
async def run_verification(
    req: VerifyRequest,
    emit: EmitFn = None,
    ov: Optional[Overrides] = None,
) -> Dict[str, Any]:
    """
    执行完整核验链路。返回可直接序列化为 VerifyResponse 的字典。

    `ov` 是本请求的凭证覆盖项（来自 Header 或后端运行时配置）。它在这里被折算成
    一份「本次请求专用」的配置快照 `eff`，后续所有模式判断、客户端构造都只用 `eff`，
    从而做到「填了 Key 立刻真跑」，且不会影响其它并发请求。

    事件（emit）时序：
        route → parse → queries → search_start/search_done ×3 → judge → complete
    """
    eff = effective_settings(ov)
    started = time.perf_counter()
    mock_index = get_mock_index()

    # ---------- 1. 地域路由 ----------
    region_match = detect_region(req.raw_input, req.region_hint)
    await _emit(
        emit,
        {
            "stage": "route",
            "label": "行政层级与政务域名路由",
            "route": region_match.to_dict(),
        },
    )

    # ---------- 2. 要素解析 ----------
    meta: PolicyMeta = parse_policy_elements(req.raw_input, region_match)
    await _emit(
        emit,
        {"stage": "parse", "label": "政策要素解析", "policy_meta": meta.to_dict()},
    )

    # ---------- 3. 案例匹配（Mock 专用，真实模式下仅作备注） ----------
    case = mock_index.get_case(req.case_id) if req.case_id else None
    if case is None:
        case = mock_index.match_case(req.raw_input, region_match.region.key)

    # ---------- 4. 三路检索词裂变 ----------
    queries = build_queries(meta, region_match)

    # 并发策略由「引擎与密钥设置」面板决定：三路（默认完整链路）/ 双路 / 单路。
    # 少跑一路不是"更快地得到同样的结论"，而是证据强度下降，因此必须在 notes 里写清楚。
    concurrency_plan = getattr(eff, "concurrency", "three_way") or "three_way"
    plan_limit = {"three_way": 3, "two_way": 2, "single": 1}.get(concurrency_plan, 3)
    plan_note = ""
    if plan_limit < len(queries):
        queries = queries[:plan_limit]
        plan_note = (
            f"检索并发策略为「{ {'three_way': '三路并发穿透', 'two_way': '双路校验', 'single': '单路快速'}.get(concurrency_plan, concurrency_plan) }」，"
            f"本次仅执行 {plan_limit} 路检索，证据覆盖面相应收窄。"
        )

    await _emit(
        emit,
        {
            "stage": "queries",
            "label": "三路检索词裂变",
            "queries": [q.to_dict() for q in queries],
        },
    )

    # ---------- 5. 三路检索（真实 / Mock 自动降级，真并发） ----------
    client = YuanbaoSearchClient(config=eff)

    async def _run_channel(spec) -> ChannelSearchResult:
        await _emit(
            emit,
            {
                "stage": "search_start",
                "label": f"通道 {spec.step}：{spec.channel}",
                "step": spec.step,
                "channel": spec.channel,
                "query": spec.query,
            },
        )
        result = await client.search_channel(
            spec,
            mock_case=case,
            meta=meta.to_dict(),
            force_mock=not eff.is_real_api,
        )
        await _emit(
            emit,
            {"stage": "search_done", "label": spec.channel, "trace": result.to_dict()},
        )
        return result

    traces: List[ChannelSearchResult] = []
    try:
        # 三路必须真并发：串行时最坏情况是 3 × 超时，页面会长时间停在 Loading。
        gathered = await asyncio.gather(
            *[_run_channel(spec) for spec in queries], return_exceptions=True
        )
        for spec, item in zip(queries, gathered):
            if isinstance(item, BaseException):
                failed = ChannelSearchResult(
                    step=spec.step,
                    channel=spec.channel,
                    channel_key=spec.channel_key,
                    query=spec.query,
                    purpose=spec.purpose,
                    hit_count=0,
                    key_finding="该通道执行异常，已跳过，不影响其余两路判定。",
                    hits=[],
                    provider="error",
                )
                failed.degraded = True
                failed.error = f"{type(item).__name__}: {item}"[:200]
                traces.append(failed)
            else:
                traces.append(item)
        traces.sort(key=lambda item: item.step)
    finally:
        await client.aclose()

    # ---------- 5.5 口语词 → 焦点政策锁定 ----------
    # 用户输入「电瓶车」时，被核验的对象不是这三个字，而是检索命中的
    # 《湖南省电动自行车管理办法》。锁定后再判定，才能避免"把口语词当公文名"。
    locked_title = ""
    if meta.is_short_query and not meta.title:
        locked_title = resolve_focus_policy(meta, traces)

    # ---------- 6. 效力判定 ----------
    # 演示模式且命中案例：直接用案例中已人工核验的结论，保证时间轴/Diff/信源自洽
    if not eff.is_real_api and case is not None:
        judged = judge_from_case(case)
        judge_source = "mock_case"
    else:
        # 判定完全由确定性规则引擎完成：四态门槛 + 铁证锚定，不依赖任何外部模型
        judged = judge_from_evidence(meta, region_match, traces)
        judge_source = "rules"

    # 真实检索 + 用户显式点选预设案例：合并专家校对的时间轴与条款对照，
    # 但检索证据、命中数、耗时仍来自本次实时抓取（两者互补，不是替换）。
    if eff.is_real_api and case is not None and req.case_id:
        curated = judge_from_case(case)
        judged["timeline"] = curated["timeline"]
        judged["clause_diffs"] = curated["clause_diffs"]
        judged["validity_result"] = curated["validity_result"]
        judge_source = "real_search+curated"


    await _emit(
        emit,
        {
            "stage": "judge",
            "label": "四维状态机判定与条款比对",
            "validity_result": judged["validity_result"],
            "judge_source": judge_source,
        },
    )

    # ---------- 7. 组装响应 ----------
    policy_meta = meta.to_dict()
    if case is not None and (not eff.is_real_api or req.case_id):
        # 演示模式，或用户显式点选了预设案例：用案例中人工校对的元数据覆盖解析结果。
        # 自由输入（未点选案例）时保持实时解析值，避免把案例数据混进真实结论。
        curated = case.get("policy_meta", {})
        for key, value in curated.items():
            if value:
                policy_meta[key] = value

    notes: List[str] = []
    if not eff.is_real_api:
        notes.append(
            "当前为【高仿真演示模式】：检索结果来自内置案例库，链接为官方网站栏目入口，"
            "用于在未配置 Key 时完整演示产品链路。"
        )
        if case is not None:
            notes.append(f"已匹配内置案例：{case.get('preset_label', case.get('id', ''))}（{case.get('id', '')}）。")
        else:
            notes.append(
                "未匹配到内置案例，本次仅演示链路：命中结果为占位数据，"
                "判定按零幻觉原则输出「存疑待人工核验」。"
            )
    else:
        notes.append(
            f"真实接口模式：provider = {eff.resolved_provider}，"
            f"超时 {eff.http_timeout}s，重试 {eff.http_retries} 次。"
        )

    if meta.is_short_query:
        terms = "、".join(meta.search_terms) or meta.topic
        notes.append(f"输入为口语/主题词，已自动映射为法定术语「{terms}」后检索。")
        if locked_title:
            notes.append(f"已从政务原文通道锁定焦点政策《{locked_title}》，后续判定均针对该文件。")
        else:
            notes.append("未从检索结果中锁定唯一焦点政策，按主题综合判断并标注证据强度。")

    if plan_note:
        notes.append(plan_note)

    degraded_steps = [t.step for t in traces if t.degraded]
    if degraded_steps:
        notes.append(
            "以下通道在真实调用中失败并已自动降级到本地引擎："
            + "、".join(f"通道 {s}" for s in degraded_steps)
        )
    if judge_source == "real_search+curated":
        notes.append("该案例的判定结论与条款对照采用专家校对版本，检索证据、命中数与耗时来自本次实时抓取。")

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    response: Dict[str, Any] = {
        "code": 200,
        "mode": eff.search_mode,
        "provider": traces[0].provider if traces else "mock",
        "elapsed_ms": elapsed_ms,
        "cached": False,
        "policy_meta": policy_meta,
        "region_route": region_match.to_dict(),
        "validity_result": judged["validity_result"],
        "search_trace": [t.to_dict() for t in traces],
        "timeline": judged["timeline"],
        "clause_diffs": judged["clause_diffs"],
        "sources": judged["sources"],
        "queries": [q.to_dict() for q in queries],
        "notes": notes,
        "matched_case_id": case.get("id") if (case and not eff.is_real_api) else None,
        "judge_source": judge_source,
        # 实操避坑与行动指引：仅预设案例带专家文案，自由输入由前端按状态兜底
        "compliance_guide": (case or {}).get("compliance_guide") if case else None,
    }

    await _emit(emit, {"stage": "complete", "label": "核验完成", "result": response})
    return response


# ---------------------------------------------------------------------------
# 接口实现
# ---------------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse, summary="运行模式与自检")
async def health() -> Dict[str, Any]:
    info = settings.describe()
    return {
        "code": 200,
        "status": "ok",
        "app": settings.app_name,
        "version": settings.version,
        "case_count": len(get_mock_index().cases),
        "server_time": datetime.now().isoformat(timespec="seconds"),
        **info,
    }


@router.get("/regions", response_model=RegionsResponse, summary="全国行政区划与快捷预设")
async def regions() -> Dict[str, Any]:
    payload = list_regions()
    payload["code"] = 200
    payload["presets"] = get_mock_index().presets()
    return payload


@router.post("/policy/parse", response_model=ParseResponse, summary="政策要素与地域自动解析")
async def parse_policy(payload: ParseRequest) -> Dict[str, Any]:
    if not payload.raw_input.strip():
        raise HTTPException(status_code=422, detail="raw_input 不能为空")

    region_match = detect_region(payload.raw_input, payload.region_hint)
    meta = parse_policy_elements(payload.raw_input, region_match)
    queries = build_queries(meta, region_match)

    notes: List[str] = []
    if not meta.doc_number:
        notes.append("未识别到发文字号，检索词将退化为「标题 + 有效期」模式，建议补充文号以提高命中率。")
    if meta.date_source != "explicit":
        notes.append("未识别到精确印发日期；系统不会用文号年份臆造日期。")
    if meta.expiry_note:
        notes.append(meta.expiry_note)

    return {
        "code": 200,
        "policy_meta": meta.to_dict(),
        "region_route": region_match.to_dict(),
        "queries": [q.to_dict() for q in queries],
        "notes": notes,
    }


@router.post("/policy/verify", response_model=VerifyResponse, summary="【核心】三路求证与效力判定")
async def verify(request: Request, payload: VerifyRequest) -> Dict[str, Any]:
    if not payload.raw_input.strip():
        raise HTTPException(status_code=422, detail="raw_input 不能为空")

    # 凭证优先级：本次请求头 > 后端运行时内存 > .env（详见 core/runtime_config.py）
    ov = overrides_from_headers(request.headers)
    eff = effective_settings(ov)

    key = _cache_key(payload, f"{ov.fingerprint()}:{eff.search_mode}:{getattr(eff, 'concurrency', 'three_way')}")
    cached = _cache_get(key)
    if cached is not None:
        result = dict(cached)
        result["cached"] = True
        return result

    try:
        result = await run_verification(payload, ov=ov)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都收敛为 200 兜底结果
        return _safe_verification_fallback(payload, exc, eff)

    _cache_put(key, result)
    return result


@router.post("/policy/verify/stream", summary="流式思考链（SSE）")
async def verify_stream(request: Request, payload: VerifyRequest) -> StreamingResponse:
    """
    以 Server-Sent Events 推送每一个推理阶段。

    前端用 fetch + ReadableStream 消费（因为 EventSource 不支持 POST）。
    事件名即 stage：route / parse / queries / search_start / search_done / judge / complete / error
    """
    if not payload.raw_input.strip():
        raise HTTPException(status_code=422, detail="raw_input 不能为空")

    ov = overrides_from_headers(request.headers)
    queue: "asyncio.Queue[Optional[Dict[str, Any]]]" = asyncio.Queue()

    async def emit(event: Dict[str, Any]) -> None:
        await queue.put(event)

    async def worker() -> None:
        try:
            await run_verification(payload, emit, ov=ov)
        except Exception as exc:  # noqa: BLE001
            await queue.put(
                {
                    "stage": "error",
                    "label": "链路异常",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            await queue.put(None)

    async def event_generator():
        task = asyncio.create_task(worker())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                stage = str(item.get("stage", "message"))
                yield f"event: {stage}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            await task

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/policy/diff", response_model=DiffResponse, summary="新旧条款字符级 Diff")
async def policy_diff(payload: DiffRequest) -> Dict[str, Any]:
    diffs: List[Dict[str, Any]] = []
    for pair in payload.pairs:
        old = str(pair.get("old", ""))
        new = str(pair.get("new", ""))
        segments = build_diff_segments(old, new)
        diffs.append(
            {
                "dimension": str(pair.get("dimension", "未命名维度")),
                "old_clause": old,
                "new_clause": new,
                "impact_level": str(pair.get("impact_level", "待评估")),
                "old_segments": segments["old_segments"],
                "new_segments": segments["new_segments"],
            }
        )
    return {"code": 200, "diffs": diffs}


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------
# 报告里使用纯文本状态标记，避免导出文件出现彩色 Emoji（截图与打印都不合适）
_STATUS_TAG = {"VALID": "现行有效", "REPLACED": "部分失效", "EXPIRED": "已废止", "UNCERTAIN": "存疑"}


def _report_title(meta: Dict[str, Any]) -> str:
    """报告标题：优先公文名，其次锁定政策，最后退化为主题词，绝不输出空标题。"""
    return (
        str(meta.get("title") or "").strip()
        or str(meta.get("locked_policy_title") or "").strip()
        or (f"「{meta.get('topic')}」相关政策" if meta.get("topic") else "未命名政策")
    )


def _safe_filename(text: str, fallback: str = "政策效力核验报告") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", text or "").strip("_")
    return (cleaned or fallback)[:60]


def build_markdown_report(result: Dict[str, Any], include_disclaimer: bool = True) -> str:
    """把核验结果渲染成可归档、可粘贴进 CSDN 的 Markdown 报告。"""
    meta = result.get("policy_meta", {})
    validity = result.get("validity_result", {})
    comparison = validity.get("hallucination_comparison", {})
    code = validity.get("status_code", "UNCERTAIN")
    tag = _STATUS_TAG.get(code, "存疑")
    report_title = _report_title(meta)

    lines: List[str] = []
    lines.append(f"# 政策效力核验报告：{report_title}")
    lines.append("")
    lines.append(
        f"> 由「政策过期检测器 Policy Validity Detector」生成 ｜ "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ｜ "
        f"核验方式：元宝搜索多路交叉求证引擎"
    )
    lines.append("")

    lines.append("## 一、判定结论")
    lines.append("")
    lines.append(f"### 【{tag}】{validity.get('status_label', '存疑待人工核验')}")
    lines.append("")
    lines.append(
        f"- **可信度**：{validity.get('confidence_score', 0)} / 100"
        f"　**权威度**：{validity.get('authority_score', 0)} / 100"
    )
    lines.append(f"- **核心结论**：{validity.get('summary', '')}")
    lines.append("")

    lines.append("## 二、政策要素")
    lines.append("")
    lines.append("| 字段 | 内容 |")
    lines.append("| --- | --- |")
    for label, key in (
        ("政策名称", "title"),
        ("发文字号", "doc_number"),
        ("发文机关", "department"),
        ("所属地区", "region"),
        ("行政层级", "admin_level"),
        ("印发日期", "publish_date"),
        ("标注有效期", "stated_expiry"),
        ("政务网域名", "gov_domain"),
    ):
        value = meta.get(key) or ("—" if key != "title" else report_title)
        lines.append(f"| {label} | {value} |")
    lines.append("")

    lines.append("## 三、「少点幻觉，多点搜索」对比")
    lines.append("")
    lines.append("### 裸大模型盲猜回答（未接入搜索，存在过期幻觉）")
    lines.append("")
    lines.append(f"> {comparison.get('naive_llm_answer', '（无）')}")
    lines.append("")
    lines.append("### 元宝搜索三路求证结论")
    lines.append("")
    lines.append(f"> {comparison.get('yuanbao_verified_answer', '（无）')}")
    lines.append("")

    lines.append("## 四、三路检索思考链")
    lines.append("")
    for step in result.get("search_trace", []):
        lines.append(f"### Step {step.get('step')}：{step.get('channel')}")
        lines.append("")
        lines.append(f"- **检索式**：`{step.get('query', '')}`")
        lines.append(f"- **命中数**：{step.get('hit_count', 0)}（耗时 {step.get('latency_ms', 0)} ms）")
        lines.append(f"- **关键发现**：{step.get('key_finding', '')}")
        for hit in step.get("hits", []):
            lines.append(
                f"  - [{hit.get('title', '')}]({hit.get('url', '')})"
                f" ｜{hit.get('site', '')}｜{hit.get('trust_level', '')}｜{hit.get('publish_time', '')}"
            )
        lines.append("")

    lines.append("## 五、政策生命周期时间轴")
    lines.append("")
    lines.append("| 日期 | 事件 | 依据文件 |")
    lines.append("| --- | --- | --- |")
    for node in result.get("timeline", []):
        lines.append(f"| {node.get('date', '')} | {node.get('event', '')} | {node.get('doc', '')} |")
    lines.append("")

    lines.append("## 六、新旧条款红绿对比")
    lines.append("")
    for diff in result.get("clause_diffs", []):
        lines.append(f"### {diff.get('dimension', '')}　`{diff.get('impact_level', '')}`")
        lines.append("")
        lines.append("| 旧规（已失效口径） | 新规（现行口径） |")
        lines.append("| --- | --- |")
        old = str(diff.get("old_clause", "")).replace("|", "\\|")
        new = str(diff.get("new_clause", "")).replace("|", "\\|")
        lines.append(f"| {old} | {new} |")
        lines.append("")

    lines.append("## 七、权威信源")
    lines.append("")
    for idx, source in enumerate(result.get("sources", []), start=1):
        lines.append(
            f"{idx}. [{source.get('title', '')}]({source.get('url', '')})"
            f" — {source.get('source_type', '')}｜{source.get('trust_level', '')}｜{source.get('publish_time', '')}"
        )
    lines.append("")

    if include_disclaimer:
        lines.append("---")
        lines.append("")
        lines.append("### 数据与免责说明")
        lines.append("")
        for note in result.get("notes", []):
            lines.append(f"- {note}")
        lines.append(
            "- 本报告由 Agent 自动生成，用于辅助判断政策时效性，不替代主管部门的正式答复。"
            "涉及资金申报、资格认定的，务必以政务网原文与当期申报通知为准。"
        )
        lines.append("")

    return "\n".join(lines)


@router.post("/export/report", response_model=ReportResponse, summary="导出 Markdown 核验报告")
async def export_report(payload: ReportRequest) -> Dict[str, Any]:
    result = payload.result or {}
    if not isinstance(result, dict) or "validity_result" not in result:
        raise HTTPException(status_code=422, detail="result 必须是 /policy/verify 的完整返回体")

    markdown = build_markdown_report(result, payload.include_disclaimer)
    title = _report_title(result.get("policy_meta", {}))
    filename = f"{_safe_filename('政策效力核验报告_' + title)}_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    return {
        "code": 200,
        "filename": filename,
        "markdown": markdown,
        "bytes": len(markdown.encode("utf-8")),
    }


# ---------------------------------------------------------------------------
# 引擎与密钥设置（运行时热插拔）
# ---------------------------------------------------------------------------
# 设计要点：
# 1. 只回掩码，永不回明文；`.env` 里的 Key 连掩码都只在 effective 段出现一次。
# 2. 保存与测试分离：/settings/test 不写内存，允许"先测后存"，
#    避免用户把一把错 Key 存进运行时后污染后续所有请求。
# 3. 运行时配置只活在内存里，进程重启即失效；前端另有 localStorage 兜底。
# ---------------------------------------------------------------------------
@router.get("/settings/status", response_model=SettingsStatusResponse, summary="引擎与密钥配置状态")
async def settings_status(request: Request) -> Dict[str, Any]:
    """返回三层配置的当前生效情况（全部掩码），供前端回显与徽章判断。"""
    data = describe_status(overrides_from_headers(request.headers))
    data["code"] = 200
    return data


@router.post("/settings/config", response_model=SettingsStatusResponse, summary="保存运行时密钥配置")
async def settings_config(payload: SettingsConfigRequest) -> Dict[str, Any]:
    """
    把前端填写的 Key 写入后端运行时内存，立即对后续请求生效（无需重启）。

    字段为 None 表示"本次不改动"，传空字符串表示"清除该项"。
    """
    patch: Dict[str, Any] = {}
    for name in ("yuanbao_api_key", "yuanbao_base_url", "concurrency"):
        value = getattr(payload, name, None)
        if value is not None:
            patch[name] = value

    if patch:
        set_runtime(patch)
        # 换 Key / 换并发策略后，旧缓存必须失效：否则用户会看到"新 Key 没生效"。
        _CACHE.clear()

    data = describe_status()
    data["code"] = 200
    # 保存后立刻告诉前端"到底有没有真的切到真实接口"，避免"填了但还在 Mock"的误解
    server = data.get("server", {})
    data["message"] = (
        f"配置已生效：{server.get('provider_label')}（{server.get('mode_label')}）"
        if server.get("search_configured")
        else "配置已保存，但搜索凭证仍不完整，当前继续运行在高仿真演示模式。"
    )
    return data


@router.delete("/settings/config", response_model=SettingsStatusResponse, summary="清除运行时密钥配置")
async def settings_clear() -> Dict[str, Any]:
    """清除运行时配置，回落到 `.env`（前端 localStorage 需同步清除）。"""
    clear_runtime()
    _CACHE.clear()
    data = describe_status()
    data["code"] = 200
    data["message"] = "已清除自定义配置，回落到 .env / 内置引擎。"
    return data


@router.post("/settings/test", response_model=SettingsTestResponse, summary="测试接口连通性")
async def settings_test(request: Request, payload: SettingsTestRequest) -> Dict[str, Any]:
    """
    用"请求体里的候选配置 > 请求头 > 运行时 > .env"发起一次真实的轻量检索探测。

    只验证元宝搜索通道：判定与条款对比完全由确定性规则引擎完成，不依赖任何外部模型，
    因此这里失败只可能是 Key / 端点 / 网络的问题，排查方向唯一。
    """
    header_ov = overrides_from_headers(request.headers)

    def pick(name: str) -> str:
        return str(getattr(payload, name, None) or getattr(header_ov, name, "") or "")

    ov = Overrides(
        yuanbao_api_key=pick("yuanbao_api_key"),
        yuanbao_base_url=pick("yuanbao_base_url"),
    )
    eff = effective_settings(ov)

    client = YuanbaoSearchClient(config=eff)
    started = time.perf_counter()
    try:
        search_res = await client.probe()
    finally:
        await client.aclose()

    total_ms = int((time.perf_counter() - started) * 1000)
    search_ok = bool(search_res.get("ok"))

    if search_ok:
        message = f"连接成功！元宝搜索接口响应正常（端到端耗时 {total_ms} ms）"
    else:
        message = f"搜索接口未通过：{search_res.get('message', '')}"

    return {
        "code": 200,
        "ok": search_ok,
        "latency_ms": total_ms,
        "message": message,
        "provider": eff.resolved_provider,
        "model": None,
        "search": search_res,
    }


# ---------------------------------------------------------------------------
# 开放接口调试示例（由后端生成，前端不硬编码任何示例数据）
# ---------------------------------------------------------------------------
@router.get("/openapi/snippets", response_model=SnippetsResponse, summary="开放接口调用示例")
async def openapi_snippets(request: Request) -> Dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    endpoint = f"{base}/api/v1/policy/verify"
    sample = "《北京市关于加快新能源汽车推广应用的实施意见》（京政办发〔2020〕12号）"

    curl = (
        f"curl -X POST '{endpoint}' \\\n"
        f"  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"raw_input\": \"{sample}\", \"region_hint\": \"auto\"}}'"
    )

    python = (
        "import httpx, json\n\n"
        f'resp = httpx.post("{endpoint}", json={{\n'
        f'    "raw_input": "{sample}",\n'
        '    "region_hint": "auto",\n'
        "}, timeout=60)\n"
        "data = resp.json()\n"
        'print(data["validity_result"]["status_label"])\n'
        'print(data["validity_result"]["summary"])'
    )

    javascript = (
        f'const resp = await fetch("{endpoint}", {{\n'
        '  method: "POST",\n'
        '  headers: { "Content-Type": "application/json" },\n'
        f"  body: JSON.stringify({{ raw_input: \"{sample}\", region_hint: \"auto\" }}),\n"
        "});\n"
        "const data = await resp.json();\n"
        'console.log(data.validity_result.status_code);'
    )

    return {
        "code": 200,
        "base_url": base,
        "endpoints": [
            {"method": "POST", "path": "/api/v1/policy/verify", "desc": "三路求证与效力判定（核心）"},
            {"method": "POST", "path": "/api/v1/policy/verify/stream", "desc": "同上的 SSE 流式版本"},
            {"method": "POST", "path": "/api/v1/policy/parse", "desc": "仅解析政策要素与检索词"},
            {"method": "GET", "path": "/api/v1/regions", "desc": "全国行政区划与政务网域名映射"},
            {"method": "POST", "path": "/api/v1/policy/diff", "desc": "新旧条款字符级 Diff"},
            {"method": "POST", "path": "/api/v1/export/report", "desc": "导出 Markdown 核验报告"},
            {"method": "GET", "path": "/docs", "desc": "Swagger 交互式文档"},
        ],
        "curl": curl,
        "python": python,
        "javascript": javascript,
    }
