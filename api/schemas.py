"""
api/schemas.py —— Pydantic 请求 / 响应数据模型
================================================================================

这一层是"对外契约"。所有字段都与 mock/national_cases.json、core 引擎的输出严格对齐，
并通过 `/docs` 自动生成 Swagger 文档，方便第三方系统与其它智能体直接接入。

设计约定
--------
1. 响应模型一律 `extra="allow"`：允许引擎补充实验性字段而不破坏契约；
2. 所有可选字段给出默认值：Mock 数据里缺字段时不会 500；
3. 枚举值统一用字符串 + Literal 约束，便于前端做状态映射。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 通用子结构
# ---------------------------------------------------------------------------
class RegionRoute(BaseModel):
    """地域路由结果：告诉调用方"这次到底去哪查"。"""

    model_config = ConfigDict(extra="allow")

    region_key: str = Field(default="", description="行政区划唯一键，如 hangzhou")
    region_name: str = Field(default="", description="行政区划全称，如 杭州市")
    admin_level: str = Field(default="", description="行政层级，如 地市级")
    gov_domain: str = Field(default="", description="政务网主域名，如 hangzhou.gov.cn")
    site_filter: str = Field(default="", description="检索用 site 过滤域名")
    confidence: float = Field(default=0.0, ge=0, le=1, description="路由置信度")
    matched_by: str = Field(default="", description="命中方式：user_hint / doc_prefix / text_alias / fallback")
    evidence: str = Field(default="", description="命中依据的可读说明")


class PolicyMeta(BaseModel):
    """政策结构化要素。"""

    model_config = ConfigDict(extra="allow")

    title: str = ""
    doc_number: str = ""
    region: str = ""
    admin_level: str = ""
    department: str = ""
    publish_date: str = Field(default="", description="精确印发日；无法确定时为空字符串，绝不臆造")
    stated_expiry: str = Field(default="", description="原文标注的有效期截止日")
    doc_year: int = 0
    is_trial: bool = False
    topic: str = ""
    region_key: str = ""
    gov_domain: str = ""
    site_filter: str = ""
    region_confidence: float = 0.0
    matched_by: str = ""
    date_source: str = Field(default="unknown", description="explicit / derived / unknown")
    expiry_note: str = Field(default="", description="有效期推算说明或待核验线索")
    is_short_query: bool = Field(default=False, description="输入是否为口语/主题短词")
    search_terms: List[str] = Field(default_factory=list, description="口语词映射后的法定检索术语")
    policy_category: str = Field(default="generic", description="subsidy / regulation / generic")
    locked_policy_title: str = Field(default="", description="从政务原文通道锁定的焦点政策标题")


class HallucinationComparison(BaseModel):
    """「少点幻觉，多点搜索」的核心对比区。"""

    model_config = ConfigDict(extra="allow")

    naive_llm_answer: str = Field(default="", description="不联网大模型容易给出的错误回答")
    yuanbao_verified_answer: str = Field(default="", description="元宝搜索三路求证后的真实结论")


class ValidityResult(BaseModel):
    """效力判定结果。"""

    model_config = ConfigDict(extra="allow")

    status_code: Literal["VALID", "REPLACED", "EXPIRED", "UNCERTAIN"] = "UNCERTAIN"
    status_label: str = ""
    status_color: str = Field(default="slate", description="前端配色：emerald / amber / red / slate")
    status_emoji: str = ""
    confidence_score: int = Field(default=0, ge=0, le=100)
    authority_score: int = Field(default=0, ge=0, le=100)
    summary: str = ""
    hallucination_comparison: HallucinationComparison = Field(default_factory=HallucinationComparison)


class SearchHitModel(BaseModel):
    """单条检索命中。"""

    model_config = ConfigDict(extra="allow")

    title: str = ""
    url: str = ""
    snippet: str = ""
    site: str = ""
    publish_time: str = ""
    source_type: str = ""
    trust_level: str = ""


class SearchTraceStep(BaseModel):
    """三路思考链中的一步。"""

    model_config = ConfigDict(extra="allow")

    step: int = 0
    channel: str = ""
    channel_key: str = ""
    query: str = ""
    purpose: str = ""
    hit_count: int = 0
    key_finding: str = ""
    hits: List[SearchHitModel] = Field(default_factory=list)
    provider: str = "mock"
    degraded: bool = False
    error: Optional[str] = None
    latency_ms: int = 0


class TimelineNode(BaseModel):
    """政策生命周期时间轴节点。"""

    model_config = ConfigDict(extra="allow")

    date: str = ""
    event: str = ""
    doc: str = ""
    status: Literal[
        "historical", "revised", "expired", "abolished", "current", "uncertain", "conflicting"
    ] = "historical"


class DiffSegment(BaseModel):
    op: Literal["equal", "delete", "insert"]
    text: str


class ClauseDiff(BaseModel):
    """新旧条款红绿 Diff。"""

    model_config = ConfigDict(extra="allow")

    dimension: str = ""
    old_clause: str = ""
    new_clause: str = ""
    impact_level: str = ""
    old_segments: List[DiffSegment] = Field(default_factory=list)
    new_segments: List[DiffSegment] = Field(default_factory=list)


class SourceItem(BaseModel):
    """权威信源。"""

    model_config = ConfigDict(extra="allow")

    title: str = ""
    url: str = ""
    source_type: str = ""
    publish_time: str = ""
    trust_level: str = ""


class QueryOut(BaseModel):
    """一条检索指令（供调试面板展示）。"""

    model_config = ConfigDict(extra="allow")

    step: int = 0
    channel: str = ""
    channel_key: str = ""
    query: str = ""
    purpose: str = ""
    expected: str = ""
    site_filter: str = ""


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ParseRequest(BaseModel):
    raw_input: str = Field(
        ...,
        min_length=2,
        max_length=4000,
        description="政策标题、发文字号或正文片段",
        examples=["杭州市人民政府办公厅关于印发杭州市加快推进跨境电子商务高质量发展实施意见的通知（杭政办函〔2021〕35号）"],
    )
    region_hint: str = Field(
        default="auto",
        description="地域提示：auto 自动识别，或传 beijing / hangzhou / zhejiang 等行政区划 key",
    )


class VerifyRequest(BaseModel):
    raw_input: str = Field(..., min_length=2, max_length=4000)
    region_hint: str = Field(default="auto")
    case_id: Optional[str] = Field(
        default=None,
        description="指定内置演示案例 ID（前端「一键演示」使用）；留空则自动匹配",
    )


class DiffRequest(BaseModel):
    """独立的条款对比接口请求体。"""

    pairs: List[Dict[str, str]] = Field(
        ...,
        description='[{"dimension":"补贴标准","old":"旧条款","new":"新条款"}]',
        examples=[[{"dimension": "补贴标准", "old": "按设备投资额10%补贴", "new": "按投资额分档，最高1000万元"}]],
    )


class ReportRequest(BaseModel):
    """导出 Markdown 核验报告。"""

    result: Dict[str, Any] = Field(..., description="/policy/verify 的完整返回体")
    include_disclaimer: bool = Field(default=True, description="是否附带数据来源与免责声明")


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------
class ParseResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    policy_meta: PolicyMeta
    region_route: RegionRoute
    queries: List[QueryOut] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class VerifyResponse(BaseModel):
    """/policy/verify 的核心响应体。"""

    model_config = ConfigDict(extra="allow")

    code: int = 200
    mode: Literal["real_api", "mock_fallback"] = "mock_fallback"
    provider: str = Field(default="mock", description="tencent_cloud / generic / mcp / mock")
    elapsed_ms: int = 0
    cached: bool = False

    policy_meta: PolicyMeta
    region_route: RegionRoute
    validity_result: ValidityResult

    search_trace: List[SearchTraceStep] = Field(default_factory=list)
    timeline: List[TimelineNode] = Field(default_factory=list)
    clause_diffs: List[ClauseDiff] = Field(default_factory=list)
    sources: List[SourceItem] = Field(default_factory=list)

    queries: List[QueryOut] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    matched_case_id: Optional[str] = None


class RegionsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    counts: Dict[str, int] = Field(default_factory=dict)
    provinces: List[Dict[str, Any]] = Field(default_factory=list)
    cities: List[Dict[str, Any]] = Field(default_factory=list)
    ministries: List[Dict[str, Any]] = Field(default_factory=list)
    city_by_parent: Dict[str, List[Dict[str, Any]]] = Field(default_factory=dict)
    quick_presets: List[Dict[str, str]] = Field(default_factory=list)
    presets: List[Dict[str, str]] = Field(default_factory=list, description="内置演示案例列表")
    notes: List[str] = Field(default_factory=list)


class ReportResponse(BaseModel):
    code: int = 200
    filename: str = ""
    markdown: str = ""
    bytes: int = 0


class DiffResponse(BaseModel):
    code: int = 200
    diffs: List[ClauseDiff] = Field(default_factory=list)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    status: str = "ok"
    app: str = ""
    version: str = ""
    mode: str = ""
    mode_label: str = ""
    provider: str = ""
    provider_label: str = ""
    search_configured: bool = False
    demo_mode: bool = False
    case_count: int = 0
    timeout: float = 0
    retries: int = 0
    server_time: str = ""


class SnippetsResponse(BaseModel):
    """开放接口调试抽屉所需的调用示例（完全由后端生成，前端不硬编码）。"""

    code: int = 200
    base_url: str = ""
    endpoints: List[Dict[str, str]] = Field(default_factory=list)
    curl: str = ""
    python: str = ""
    javascript: str = ""


class WebhookPayload(BaseModel):
    """企业微信 / 飞书机器人回调的宽松请求体。"""

    model_config = ConfigDict(extra="allow")

    msg_type: str = Field(default="text", description="text / markdown / interactive")
    text: Optional[str] = Field(default=None, description="纯文本内容")
    content: Optional[str] = Field(default=None, description="富文本 / markdown 内容")
    policy: Optional[str] = Field(default=None, description="待核验的政策文本")
    token: Optional[str] = Field(default=None, description="回调校验 Token")


class WebhookResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    msg_type: str = "markdown"
    content: str = ""


# ---------------------------------------------------------------------------
# 引擎与密钥设置（运行时热插拔）
# ---------------------------------------------------------------------------
class SettingsConfigRequest(BaseModel):
    """
    前端「引擎与密钥设置」提交的配置。

    语义约定：字段为 None 表示"本次不改动"，传空字符串表示"清除该项"。
    这样同一个接口既能做保存，也能做单字段清除，不需要额外的 PATCH 语义。
    """

    model_config = ConfigDict(extra="allow")

    yuanbao_api_key: Optional[str] = Field(
        default=None, description="元宝搜索服务 API KEY；也兼容 SecretId|SecretKey 的成对写法"
    )
    yuanbao_base_url: Optional[str] = Field(default=None, description="搜索接口 Base URL；留空使用官方默认端点")
    concurrency: Optional[str] = Field(default=None, description="检索并发策略：three_way / two_way / single")


class SettingsTestRequest(BaseModel):
    """连通性测试请求：可携带"尚未保存"的候选配置，便于保存前先验证。"""

    model_config = ConfigDict(extra="allow")

    yuanbao_api_key: Optional[str] = None
    yuanbao_base_url: Optional[str] = None


class SettingsStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    server: Dict[str, Any] = Field(default_factory=dict)
    runtime: Dict[str, Any] = Field(default_factory=dict)
    effective: Dict[str, Any] = Field(default_factory=dict)
    defaults: Dict[str, Any] = Field(default_factory=dict)
    headers: Dict[str, Any] = Field(default_factory=dict)


class SettingsTestResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: int = 200
    ok: bool = False
    latency_ms: int = 0
    message: str = ""
    search: Dict[str, Any] = Field(default_factory=dict)
