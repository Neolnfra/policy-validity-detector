"""
core —— 核心业务与 Agent 编排层
================================================================================

模块职责边界：
    region_router.py   全国行政区划 → 政务网域名路由
    query_builder.py   政策要素解析 + 三路检索词裂变
    yuanbao_client.py  元宝搜索适配器（腾讯云 TC3 / 通用 REST / MCP）+ Mock 降级
    validity_judge.py  效力判定、权威度评分、条款 Diff

本层不依赖 FastAPI，可被脚本、定时任务、其它框架直接复用。
"""

from core.region_router import (  # noqa: F401
    Region,
    MINISTRIES,
    PROVINCES,
    CITIES,
    QUICK_REGIONS,
    detect_region,
    list_regions,
    resolve_region,
)
from core.query_builder import (  # noqa: F401
    PolicyMeta,
    QuerySpec,
    build_queries,
    parse_policy_elements,
)
from core.yuanbao_client import (  # noqa: F401
    ChannelSearchResult,
    SearchHit,
    YuanbaoSearchClient,
    get_mock_index,
)
from core.validity_judge import (  # noqa: F401
    STATUS_CATALOG,
    build_diff_segments,
    enrich_clause_diffs,
    judge_from_case,
    judge_from_evidence,
)

__all__ = [
    "Region",
    "MINISTRIES",
    "PROVINCES",
    "CITIES",
    "QUICK_REGIONS",
    "detect_region",
    "list_regions",
    "resolve_region",
    "PolicyMeta",
    "QuerySpec",
    "build_queries",
    "parse_policy_elements",
    "ChannelSearchResult",
    "SearchHit",
    "YuanbaoSearchClient",
    "get_mock_index",
    "STATUS_CATALOG",
    "build_diff_segments",
    "enrich_clause_diffs",
    "judge_from_case",
    "judge_from_evidence",
]
