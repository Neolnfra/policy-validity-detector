"""
core/yuanbao_client.py —— 元宝搜索适配器（真实接口 + Mock 自动降级）
================================================================================

三种真实接口形态，全部通过 .env 切换，业务层无感知
--------------------------------------------------
    A. tencent_cloud  腾讯云「元宝搜索（原联网搜索 WSA）」官方 API
                      内置 TC3-HMAC-SHA256 签名实现，无需安装腾讯云 SDK
    B. generic        通用 REST 搜索网关（Bearer Token + JSON POST）
                      适配自建网关、代理层或其它厂商的 OpenAI 风格检索接口
    C. mcp            MCP over HTTP（JSON-RPC 2.0 tools/call）
                      适配已在本地/云端挂载的元宝搜索 MCP Server

降级契约（Fail-safe Contract）
------------------------------
任何一次真实调用，只要出现以下任一情况，都必须【静默降级】到本地高仿真引擎，
绝不允许把异常抛给前端：
    - 未配置凭证 / 配置不完整
    - 连接超时、DNS 失败、TLS 错误
    - HTTP 非 2xx
    - 返回体结构无法解析，或解析出的结果为空

降级不是"假装成功"：返回的 ChannelSearchResult 会带 degraded=True 与 error 原因，
前端据此在搜索链上标注"本地降级"，保证可观测性。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from config import MOCK_CASES_FILE, Settings, settings as default_settings
from core.query_builder import QuerySpec
from core.region_router import CITIES as _REGION_CITIES
from core.region_router import PROVINCES as _REGION_PROVINCES
from core.runtime_config import humanize_http_error

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class SearchHit:
    """一条检索命中结果（统一结构，屏蔽不同接口的字段差异）。"""

    title: str = ""
    url: str = ""
    snippet: str = ""
    site: str = ""
    publish_time: str = ""
    source_type: str = ""
    trust_level: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "site": self.site,
            "publish_time": self.publish_time,
            "source_type": self.source_type,
            "trust_level": self.trust_level,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SearchHit":
        return cls(
            title=str(data.get("title", "")),
            url=str(data.get("url", "")),
            snippet=str(data.get("snippet", "")),
            site=str(data.get("site", "")),
            publish_time=str(data.get("publish_time", "")),
            source_type=str(data.get("source_type", "")),
            trust_level=str(data.get("trust_level", "")),
        )


@dataclass
class ChannelSearchResult:
    """单条检索通道的执行结果。"""

    step: int
    channel: str
    channel_key: str
    query: str
    purpose: str = ""
    hit_count: int = 0
    key_finding: str = ""
    hits: List[SearchHit] = field(default_factory=list)
    provider: str = "mock"
    degraded: bool = False
    error: Optional[str] = None
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "channel": self.channel,
            "channel_key": self.channel_key,
            "query": self.query,
            "purpose": self.purpose,
            "hit_count": self.hit_count,
            "key_finding": self.key_finding,
            "hits": [h.to_dict() for h in self.hits],
            "provider": self.provider,
            "degraded": self.degraded,
            "error": self.error,
            "latency_ms": self.latency_ms,
        }


# ---------------------------------------------------------------------------
# 本地高仿真检索引擎（Mock）
# ---------------------------------------------------------------------------


class MockSearchIndex:
    """
    读取 mock/national_cases.json，对外提供两类服务：
      1. 案例匹配：把用户输入映射到最相近的内置案例；
      2. 检索模拟：按通道返回该案例预先编排的高仿真命中结果与关键发现。
    """

    def __init__(self, path: Path = MOCK_CASES_FILE) -> None:
        self.path = path
        self.cases: List[Dict[str, Any]] = []
        self.status_catalog: Dict[str, Any] = {}
        self.data_disclaimer: str = ""
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.cases = []
            return
        with self.path.open("r", encoding="utf-8") as fp:
            payload = json.load(fp)
        self.cases = payload.get("cases", [])
        self.status_catalog = payload.get("status_catalog", {})
        self.data_disclaimer = payload.get("data_disclaimer", "")

    # ---------------- 案例匹配 ----------------
    def get_case(self, case_id: str) -> Optional[Dict[str, Any]]:
        for case in self.cases:
            if case.get("id") == case_id:
                return case
        return None

    def match_case(self, raw_input: str, region_key: str = "") -> Optional[Dict[str, Any]]:
        """
        关键词加权打分匹配。

        打分规则：
            +3  命中案例关键词
            +6  属地 key 完全一致（非全国兜底时）
            +2  案例标题的核心片段出现在输入中
        阈值：总分 >= 4 才认领，避免"随便什么输入都强行套案例"。
        """
        text = (raw_input or "").strip()
        if not text:
            return None

        best: Optional[Dict[str, Any]] = None
        best_score = 0

        for case in self.cases:
            score = 0
            for keyword in case.get("keywords", []):
                if keyword and keyword in text:
                    score += 3
            if region_key and case.get("region_key") == region_key and region_key != "national":
                score += 6
            title = case.get("policy_meta", {}).get("title", "")
            if title and title[:8] and title[:8] in text:
                score += 2
            doc_number = case.get("policy_meta", {}).get("doc_number", "")
            if doc_number and doc_number in text:
                score += 8

            if score > best_score:
                best_score = score
                best = case

        return best if best_score >= 4 else None

    # ---------------- 检索模拟 ----------------
    def hits_for(self, case: Dict[str, Any], channel_key: str) -> Tuple[List[SearchHit], str, int]:
        """取某案例某通道的命中结果、关键发现、原始命中数。"""
        trace = case.get("search_trace", [])
        for step in trace:
            key = self._channel_key_of(step)
            if key == channel_key:
                hits = [SearchHit.from_dict(h) for h in step.get("hits", [])]
                return hits, step.get("key_finding", ""), int(step.get("hit_count", len(hits)))
        return [], "", 0

    @staticmethod
    def _channel_key_of(step: Dict[str, Any]) -> str:
        explicit = step.get("channel_key")
        if explicit:
            return str(explicit)
        name = step.get("channel", "")
        if "原文" in name or "有效期" in name:
            return "origin"
        if "废止" in name or "失效" in name or "清理" in name:
            return "abolish"
        return "replacement"

    def synthetic_hits(
        self,
        query: str,
        channel_key: str,
        region_domain: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[SearchHit], str]:
        """
        未匹配到内置案例时，生成"链路演示用"的占位命中。

        注意：这不是伪造结论。所有占位结果的 source_type 明确标注为
        “演示占位（未联网）”，trust_level 为 N/A，下游判定引擎会据此
        给出 UNCERTAIN，而不会凭空判定有效或失效。
        """
        meta = meta or {}
        domain = region_domain or "gov.cn"
        title = meta.get("title") or "目标政策"
        doc_number = meta.get("doc_number") or "（未识别到发文字号）"

        templates = {
            "origin": (
                f"{title}（全文）· 演示占位结果",
                f"未接入真实检索通道，本条为演示占位。若接入元宝搜索，此处应返回 site:{domain} "
                f"下的官方原文，并可在文末附则核验「{doc_number}」的有效期条款。",
                "演示占位（未联网）",
                "N/A",
            ),
            "abolish": (
                f"关于「{doc_number}」的规范性文件清理结果 · 演示占位结果",
                "未接入真实检索通道，本条为演示占位。若接入元宝搜索，此处应返回政府公报中的"
                "《废止/宣布失效文件目录》或年度清理结果公告。",
                "演示占位（未联网）",
                "N/A",
            ),
            "replacement": (
                f"{meta.get('region', '本级政府')}关于{meta.get('topic', '该事项')}的最新政策 · 演示占位结果",
                "未接入真实检索通道，本条为演示占位。若接入元宝搜索，此处应返回近两年接续政策原文"
                "及官方“一图读懂”解读。",
                "演示占位（未联网）",
                "N/A",
            ),
        }
        head, snippet, source_type, trust = templates.get(channel_key, templates["origin"])
        hits = [
            SearchHit(
                title=head,
                url=f"https://{domain}/",
                snippet=snippet,
                site=domain,
                publish_time="",
                source_type=source_type,
                trust_level=trust,
            )
        ]
        finding = (
            "演示模式且未匹配到内置案例：本次仅跑通链路，未获得可采信的一手信源，"
            "因此结论标记为「存疑待人工核验」。"
        )
        return hits, finding

    def presets(self) -> List[Dict[str, str]]:
        """前端 5 个一键演示胶囊按钮的数据源。"""
        return [
            {
                "id": c.get("id", ""),
                "label": c.get("preset_label", ""),
                "short": c.get("preset_short", ""),
                "region_key": c.get("region_key", ""),
                "region_label": c.get("region_label", ""),
                "status_code": c.get("validity_result", {}).get("status_code", ""),
                "raw_input": c.get("raw_input", ""),
            }
            for c in self.cases
        ]


_MOCK_INDEX: Optional[MockSearchIndex] = None


def get_mock_index() -> MockSearchIndex:
    global _MOCK_INDEX
    if _MOCK_INDEX is None:
        _MOCK_INDEX = MockSearchIndex()
    return _MOCK_INDEX


# ---------------------------------------------------------------------------
# 腾讯云 TC3-HMAC-SHA256 签名
# ---------------------------------------------------------------------------


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def build_tc3_headers(
    *,
    secret_id: str,
    secret_key: str,
    host: str,
    service: str,
    action: str,
    version: str,
    region: str,
    payload: str,
) -> Dict[str, str]:
    """
    生成腾讯云 API 3.0 签名头。

    参考：https://cloud.tencent.com/document/api/213/30654
    实现为标准 TC3-HMAC-SHA256，与官方 SDK 等价，无需引入 tencentcloud-sdk-python。

    注意：元宝搜索（WSA）是【无地域接口】，请求若携带 X-TC-Region 会返回
    `UnsupportedRegion: The action does not support this region.`。
    因此 region 为空时不发送该头（签名本身只覆盖 content-type 与 host，不受影响）。
    """
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")

    content_type = "application/json; charset=utf-8"
    signed_headers = "content-type;host"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    canonical_request = "\n".join(
        ["POST", "/", "", canonical_headers, signed_headers, _sha256_hex(payload.encode("utf-8"))]
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            "TC3-HMAC-SHA256",
            str(timestamp),
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    secret_date = _hmac256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac256(secret_date, service)
    secret_signing = _hmac256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    authorization = (
        f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    headers = {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": host,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Timestamp": str(timestamp),
    }
    if region:
        headers["X-TC-Region"] = region
    return headers


# ---------------------------------------------------------------------------
# 返回体解析（容忍多种字段命名）
# ---------------------------------------------------------------------------
_URL_KEYS = ("url", "Url", "URL", "link", "Link", "href")
_TITLE_KEYS = ("title", "Title", "name", "Name", "headline")
_SNIPPET_KEYS = ("snippet", "Snippet", "content", "Content", "summary", "Summary", "desc", "Description", "Passage")
_DATE_KEYS = ("publish_time", "PublishTime", "date", "Date", "pubTime", "PubDate", "time")


def _pick(data: Dict[str, Any], keys: Sequence[str], default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return default


def _deep_find_doc_list(node: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """在任意嵌套的 JSON 里找到"看起来像搜索结果"的字典列表。"""
    if depth > 7:
        return []
    if isinstance(node, list):
        dicts = [item for item in node if isinstance(item, dict)]
        if dicts and any(any(k in item for k in _URL_KEYS) for item in dicts):
            return dicts
        collected: List[Dict[str, Any]] = []
        for item in node:
            collected.extend(_deep_find_doc_list(item, depth + 1))
        return collected
    if isinstance(node, dict):
        collected = []
        for value in node.values():
            collected.extend(_deep_find_doc_list(value, depth + 1))
        return collected
    return []


def _host_of(url: str) -> str:
    """从 URL 中取主机名。"""
    match = re.match(r"https?://([^/]+)", url or "")
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# 数据净网：英文镜像 / 繁体镜像 / 抓取乱码
# ---------------------------------------------------------------------------
# 英文站与繁体镜像的路径特征。这类页面在语义检索里排序往往很高，
# 但内容是英文或繁体，直接进结论会让公文名称看起来很不专业。
_MIRROR_URL_PAT = re.compile(
    r"(/english/|english\.|/gate/big5/|/big5/|/tc/|/cht/|/traditional/)", re.I
)

# 常见繁体字 → 简体字（覆盖政务公文高频字，作为无第三方依赖的兜底转换）
_TRAD_TO_SIMP = {
    "為": "为", "爲": "为", "與": "与", "這": "这", "個": "个", "們": "们",
    "時": "时", "間": "间", "於": "于", "關": "关", "廢": "废", "務": "务",
    "會": "会", "議": "议", "決": "决", "規": "规", "則": "则", "網": "网",
    "資": "资", "產": "产", "業": "业", "電": "电", "動": "动", "車": "车",
    "發": "发", "實": "实", "見": "见", "辦": "办", "廳": "厅", "聯": "联",
    "國": "国", "際": "际", "標": "标", "準": "准", "條": "条", "學": "学",
    "習": "习", "應": "应", "該": "该", "認": "认", "說": "说", "開": "开",
    "進": "进", "區": "区", "處": "处", "類": "类", "數": "数", "據": "据",
    "檢": "检", "測": "测", "詢": "询", "報": "报", "導": "导", "讀": "读",
    "圖": "图", "紙": "纸", "頁": "页", "張": "张", "傳": "传", "統": "统",
    "轉": "转", "換": "换", "顯": "显", "欄": "栏", "錄": "录", "檔": "档",
    "彙": "汇", "總": "总", "結": "结", "論": "论", "舊": "旧", "變": "变",
    "調": "调", "額": "额", "補": "补", "貼": "贴", "獎": "奖", "勵": "励",
    "稅": "税", "費": "费", "繳": "缴", "納": "纳", "證": "证", "許": "许",
    "審": "审", "監": "监", "罰": "罚", "責": "责", "義": "义", "權": "权",
    "簽": "签", "書": "书", "計": "计", "劃": "划", "執": "执", "評": "评",
    "週": "周", "階": "阶", "段": "段", "對": "对", "象": "象", "範": "范",
    "圍": "围", "適": "适", "項": "项", "據": "据", "來": "来", "源": "源",
    "憑": "凭", "刪": "删", "訂": "订", "充": "充", "延": "延", "長": "长",
    "終": "终", "體": "体", "系": "系", "億": "亿", "萬": "万", "圓": "圆",
    "舉": "举", "辦": "办", "們": "们", "麼": "么", "產": "产", "權": "权",
    "縣": "县", "鄉": "乡", "鎮": "镇", "廣": "广", "東": "东", "蘇": "苏",
    "浙": "浙", "遼": "辽", "寧": "宁", "陝": "陕", "貴": "贵", "雲": "云",
    "龍": "龙", "馬": "马", "鳥": "鸟", "魚": "鱼", "藥": "药", "醫": "医",
    "環": "环", "衛": "卫", "綠": "绿", "節": "节", "盡": "尽", "兩": "两",
    "單": "单", "雙": "双", "製": "制", "複": "复", "雜": "杂", "嚴": "严",
    "訓": "训", "練": "练", "織": "织", "繼": "继", "續": "续", "佈": "布",
    "內": "内", "冊": "册", "幹": "干", "麼": "么", "兒": "儿", "奬": "奖",
    "産": "产", "衆": "众", "臺": "台", "灣": "湾", "門": "门", "陽": "阳",
    "際": "际", "隨": "随", "險": "险", "驗": "验", "體": "体", "黨": "党",
}

# 半角/异体标点归一（､ ｡ 等全角兼容区字符）
_PUNCT_TRANS = str.maketrans({
    "､": "、", "｡": "。", "｢": "「", "｣": "」", "･": "·", "﹣": "-", "－": "-",
})

# 判定"这是繁体页"的标记字已随 has_traditional 一并移除；
# 繁简转换仍由 to_simplified 的字符映射兜底，无需保留标记集合。

_INVISIBLE_PAT = re.compile(r"[\u200b-\u200f\u2060\ufeff\xa0\u3000]+")
_GARBLED_PAT = re.compile(r"[□■◻◼◾�▯▮]{1,}")
_DASH_RUN_PAT = re.compile(r"[-—–_]{3,}")
_SPACE_RUN_PAT = re.compile(r"[ \t]{2,}")


def is_meaningless_title(title: str) -> bool:
    """
    无意义标题过滤。

    部分政府站点的 <title> 抓成了页面字段名（"索 引 号:"、"发文机关:"），
    保留下来会让信源卡看起来像坏数据。
    """
    text = re.sub(r"\s+", "", title or "")
    if not text:
        return True
    if len(text) < 6:
        return True
    return bool(re.match(r"^(索引号|发文机关|成文日期|主题分类|公开日期|有效性|废止日期)[:：]?$", text))


def is_mirror_url(url: str) -> bool:
    """英文站 / 繁体镜像站的 URL 特征判断。"""
    return bool(_MIRROR_URL_PAT.search(url or ""))


# ---------------------------------------------------------------------------
# 噪音域名与信源权威排序
# ---------------------------------------------------------------------------
# 论坛、问答、聚合站：这类页面常把一份公文转载十几次，标题一模一样，必须清掉
_NOISE_HOST_PAT = re.compile(
    r"(^|\.)(bbs|forum|tieba|zhihu|baidu|sohu|163|sina|toutiao|cnhubei|eastday)\.",
    re.I,
)

# 中央部委主站白名单（检索层级为国务院时优先保留）
_CENTRAL_HOSTS = {
    "gov.cn", "www.gov.cn", "ndrc.gov.cn", "mof.gov.cn", "mofcom.gov.cn",
    "miit.gov.cn", "most.gov.cn", "mohrss.gov.cn", "nhc.gov.cn", "mohurd.gov.cn",
    "mot.gov.cn", "moa.gov.cn", "mee.gov.cn", "mnr.gov.cn", "mwr.gov.cn",
    "mct.gov.cn", "mca.gov.cn", "moj.gov.cn", "mps.gov.cn", "mva.gov.cn",
    "mem.gov.cn", "pbc.gov.cn", "audit.gov.cn", "chinatax.gov.cn", "samr.gov.cn",
    "nfra.gov.cn", "csrc.gov.cn", "nhsa.gov.cn", "nea.gov.cn", "cnipa.gov.cn",
    "stats.gov.cn", "customs.gov.cn", "caac.gov.cn", "nmpa.gov.cn", "nda.gov.cn",
    "forestry.gov.cn",
}

# 属地文件的"实施细则"特征词：即使是区县级站点也值得保留
_LOCAL_RULE_PAT = re.compile(r"(细则|实施方案|暂行办法|试点方案|操作指南)")


def is_noise_host(host: str) -> bool:
    """论坛 / 问答 / 聚合站判断。"""
    return bool(_NOISE_HOST_PAT.search(host or ""))


def host_rank(host: str) -> int:
    """信源权威排序：中央主站 0 < 部委 1 < 各级政府 2 < 官微 3 < 其它 4。"""
    h = (host or "").lower()
    if h in {"gov.cn", "www.gov.cn"}:
        return 0
    if h in _CENTRAL_HOSTS:
        return 1
    if h.endswith(".gov.cn") or h.endswith(".gov.hk") or h.endswith(".gov.mo"):
        return 2
    if "weixin.qq.com" in h or "mp.weixin" in h:
        return 3
    return 4


# ---------------------------------------------------------------------------
# 属地白名单 / 外省黑名单
# ---------------------------------------------------------------------------
def _short_region_name(name: str) -> str:
    """北京市 → 北京；广西壮族自治区 → 广西；内蒙古自治区 → 内蒙古。"""
    return re.sub(
        r"(特别行政区|维吾尔自治区|壮族自治区|回族自治区|自治区|省|市)$", "", name or ""
    )


# 允许域名 → 所属地区 key（用于识别"这条结果其实是外省的站"）
_DOMAIN_TO_REGION: Dict[str, str] = {}
for _r in list(_REGION_PROVINCES) + list(_REGION_CITIES):
    if _r.gov_domain:
        _DOMAIN_TO_REGION[_r.gov_domain.lower()] = _r.key

# 所有省级短名（用于"标题里出现外省名"的检测）
_PROVINCE_SHORTS: List[Tuple[str, str]] = [
    (_r.key, _short_region_name(_r.name)) for _r in _REGION_PROVINCES
]

# 主要城市 → 所属省（标题里出现外省城市名同样视为串台）
_CITY_TO_PROVINCE: List[Tuple[str, str, str]] = [
    (_r.key, _short_region_name(_r.name), _r.parent) for _r in _REGION_CITIES
]


def _region_filter_context(
    meta: Optional[Dict[str, Any]],
) -> Optional[Tuple[set, set, set]]:
    """
    构造属地过滤上下文：返回 (允许域名, 禁止地名, 本地地名)。

    返回 None 表示"不需要属地过滤"（全国 / 部委级检索）。
    """
    if not meta:
        return None
    admin = str(meta.get("admin_level", "") or "")
    key = str(meta.get("region_key", "") or "")
    if admin.startswith("国务院") or key == "national" or not key:
        return None

    selected = next(
        (r for r in list(_REGION_PROVINCES) + list(_REGION_CITIES) if r.key == key), None
    )
    if selected is None:
        return None

    allowed: set = set(_CENTRAL_HOSTS)
    local_names: set = {selected.name, _short_region_name(selected.name)}
    excluded_provinces: set = set()

    if selected.tier == "city":
        parent = next((p for p in _REGION_PROVINCES if p.key == selected.parent), None)
        if parent is not None:
            allowed.add(parent.gov_domain.lower())
            local_names.update({parent.name, _short_region_name(parent.name)})
            excluded_provinces.add(parent.key)
            # 同省其它城市同样属于"本省范围"，允许出现
            for city in _REGION_CITIES:
                if city.parent == parent.key:
                    allowed.add(city.gov_domain.lower())
                    local_names.add(_short_region_name(city.name))
    else:
        # 省级：本省下辖城市的主站一并放行（如湖南省 → changsha.gov.cn）
        for city in _REGION_CITIES:
            if city.parent == selected.key:
                allowed.add(city.gov_domain.lower())
                local_names.add(_short_region_name(city.name))

    allowed.add(selected.gov_domain.lower())

    # 外省黑名单：除本省（及地市场景的父省）以外的所有省级短名
    forbidden: set = set()
    for province_key, short in _PROVINCE_SHORTS:
        if province_key in excluded_provinces or province_key == key:
            continue
        if len(short) >= 2:
            forbidden.add(short)

    # 外省城市名也列入黑名单（"海口""广州"这类出现在标题里必定是串台）
    for city_key, city_short, city_parent in _CITY_TO_PROVINCE:
        if city_key == key or city_parent == key or city_parent in excluded_provinces:
            continue
        if len(city_short) >= 2:
            forbidden.add(city_short)

    forbidden -= local_names
    return allowed, forbidden, local_names


def _host_in_allowed(host: str, allowed: set) -> bool:
    """
    主机是否在白名单内。

    注意：`gov.cn` 只能做【精确匹配】，绝不能当后缀用。
    否则 `xxx.gov.cn` 会全部命中 "endswith('.gov.cn')"，
    外省域名黑名单（hainan.gov.cn 等）就会形同虚设——这是一个真实踩过的坑。
    """
    h = (host or "").lower()
    if not h:
        return False
    for domain in allowed:
        if h == domain:
            return True
        if domain in {"gov.cn", "www.gov.cn"}:
            continue
        if h.endswith("." + domain):
            return True
    return False


def _host_is_other_region(host: str, allowed: set) -> bool:
    """域名是否明确属于另一个已收录的省 / 市。"""
    h = (host or "").lower()
    for domain, _key in _DOMAIN_TO_REGION.items():
        if (h == domain or h.endswith("." + domain)) and not _host_in_allowed(h, allowed):
            return True
    return False


def is_out_of_region(hit: SearchHit, allowed: set, forbidden: set, local_names: set) -> bool:
    """
    单条结果是否属"跨省串台"。

    三道校验：
        1. 标题/摘要出现外省地名 → 剔除（如选湖南却出现"海南交警"）；
        2. 域名明确属于其它省市政务网 → 剔除（如 hainan.gov.cn、gd.gov.cn）；
        3. 未收录的 *.gov.cn（多为本省区的区县站，如 kaifu.gov.cn）：
           标题或摘要里必须出现本地地名，否则视为来源不明的转载并剔除。
    """
    host = (hit.site or "").lower()
    blob = f"{hit.title} {hit.snippet}"

    if any(name in blob for name in forbidden):
        return True
    if _host_is_other_region(host, allowed):
        return True
    if _host_in_allowed(host, allowed):
        return False
    if host.endswith(".gov.cn") or host.endswith(".gov.hk") or host.endswith(".gov.mo"):
        return not any(name in blob for name in local_names)
    # 非政府、非官微来源：地方政策核验一律不采信
    return True


def filter_by_region(
    hits: Sequence[SearchHit], meta: Optional[Dict[str, Any]]
) -> List[SearchHit]:
    """按属地白名单过滤搜索结果；全国 / 部委级检索不过滤。"""
    context = _region_filter_context(meta)
    if context is None:
        return list(hits)
    allowed, forbidden, local_names = context
    return [hit for hit in hits if not is_out_of_region(hit, allowed, forbidden, local_names)]



def normalize_title(title: str) -> str:
    """
    标题归一化，用于去重。

    真实检索里同一份文件常有多种写法：
        《国务院关于印发〈推动…行动方案〉的通知》_机械制造与重工业_中国政府网
        国务院关于印发《推动…行动方案》的通知（国发〔2024〕7号）
        华容县政府网-国务院关于印发《推动…行动方案》的通知
    这里优先取书名号内的核心文件名，其次按 `_ - |` 截断站点后缀，
    保证同一份文件归一后得到同一个 key。
    """
    text = title or ""
    match = re.search(r"《([^》]{4,60})》", text)
    core = match.group(1) if match else re.split(r"[_\-|—]", text)[0]
    core = re.sub(r"[《》〈〉\s【】\[\]（）()、，,。；;：:·\-—_/“”\"'']", "", core)
    return core[:80]


def titles_conflict(a: str, b: str) -> bool:
    """
    判定两条标题是否指向同一份文件。

    除了完全相等，还覆盖「A 是 B 的子串」这种常见情况：
        国家发展改革委财政部关于2026年…的通知
        关于2026年…的通知
    后者是前者的子串，应视为同一文件。
    """
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 10 and len(b) >= 10 and (a in b or b in a)


def dedupe_hits(hits: Sequence[SearchHit]) -> List[SearchHit]:
    """
    同名文件只保留最权威的一条。

    真实检索里，一份国务院文件会被十几个区县政府网站机械转载，标题只差站点后缀。
    若不去重，模块 03/06 会被同一条内容刷屏。
    """
    kept: List[tuple] = []  # [(key, hit), ...]
    for hit in hits:
        key = normalize_title(hit.title) or hit.url
        dup_index = None
        for idx, (existing_key, _) in enumerate(kept):
            if titles_conflict(key, existing_key):
                dup_index = idx
                break

        if dup_index is None:
            kept.append((key, hit))
        elif host_rank(hit.site) < host_rank(kept[dup_index][1].site):
            # 同一份文件，换成更权威的来源
            kept[dup_index] = (key, hit)

    return [hit for _, hit in kept]


def is_english_title(title: str) -> bool:
    """纯英文标题（如 Policies、Laws & Policies）判定，避免英文索引页污染公文名称。"""
    letters = len(re.findall(r"[A-Za-z]", title or ""))
    cjk = len(re.findall(r"[\u4e00-\u9fa5]", title or ""))
    return letters >= 8 and cjk <= 2


def to_simplified(text: str) -> str:
    """繁体转简体 + 兼容区标点归一（字符级映射，够用且零依赖）。"""
    if not text:
        return ""
    normalized = text.translate(_PUNCT_TRANS)
    return "".join(_TRAD_TO_SIMP.get(ch, ch) for ch in normalized)


def clean_scraped_text(text: str) -> str:
    """
    清洗抓取残留：不可见字符、方框乱码、连续横杠、多余空白。

    典型脏样本："□2026-10-29 11:22 □□-□□环球网"
    清洗后："2026-10-29 11:22 环球网"
    """
    if not text:
        return ""
    cleaned = _INVISIBLE_PAT.sub(" ", str(text))
    cleaned = _GARBLED_PAT.sub(" ", cleaned)
    cleaned = _DASH_RUN_PAT.sub("—", cleaned)
    cleaned = _SPACE_RUN_PAT.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def normalize_hit_text(text: str) -> str:
    """清洗 + 繁体转简体，一步到位。"""
    return to_simplified(clean_scraped_text(text))


def _grade_trust(host: str) -> str:
    """
    按域名给真实检索结果做可信度初判。

    这一步很关键：真实模式下没有人工标注的 trust_level，
    若不评级，权威度评分会全部落在基础分上，判定结果失去区分度。
    """
    h = (host or "").lower()
    if h.endswith(".gov.cn") or h in {"gov.cn", "www.gov.cn"}:
        return "S级权威"
    if h.endswith(".gov.hk") or h.endswith(".gov.mo"):
        return "S级权威"
    if "weixin.qq.com" in h or "mp.weixin" in h:
        return "A级权威"
    return "C级（待核）"


def parse_payload_to_hits(payload: Any) -> List[SearchHit]:
    """
    把任意搜索接口的返回体转成统一的 SearchHit 列表。

    覆盖三类常见形态：
      1. 腾讯云 WSA：{"Response": {"Pages": ["{\\"title\\":..,\\"url\\":..}", ...]}}
         注意 Pages 的每个元素是【JSON 字符串】，不是纯文本，需二次解析；
      2. 标准文档列表：[{"title":..., "url":..., "content":...}, ...]
      3. 纯文本页数组：["...", ...]（逐段解析出首行标题与首个 URL）
    """
    hits: List[SearchHit] = []

    # ---------- 形态 1：腾讯云 Pages ----------
    pages = None
    if isinstance(payload, dict):
        response = payload.get("Response") or payload.get("response") or {}
        if isinstance(response, dict):
            pages = response.get("Pages") or response.get("pages")

    if isinstance(pages, list) and pages and all(isinstance(p, str) for p in pages):
        for raw in pages:
            text = str(raw).strip()
            if not text:
                continue

            obj: Optional[Dict[str, Any]] = None
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        obj = parsed
                except json.JSONDecodeError:
                    obj = None

            if obj is not None:
                url = str(obj.get("url") or "")

                # 净网 ①：丢弃英文站与繁体镜像站（/english/、/gate/big5/ 等）
                if is_mirror_url(url):
                    continue

                host = _host_of(url)
                # 净网 ②：丢弃论坛 / 问答 / 聚合站
                if is_noise_host(host):
                    continue

                raw_title = str(obj.get("title") or "")
                # 净网 ③：丢弃纯英文索引页（Policies / Laws & Policies 之类）
                if is_english_title(raw_title):
                    continue

                site_name = normalize_hit_text(str(obj.get("site") or ""))
                body = str(obj.get("content") or obj.get("passage") or "")

                # 净网 ③：清乱码 + 繁体转简体
                title = normalize_hit_text(raw_title) or normalize_hit_text(body[:40])
                if not title or is_meaningless_title(title):
                    continue

                hits.append(
                    SearchHit(
                        title=title[:160],
                        url=url,
                        # 摘要保留 1200 字：判定引擎要在这段文本里做"文号 + 废止词"同现检测，
                        # 截得太短会漏掉正文中后段出现的文号，直接拉低召回。
                        snippet=normalize_hit_text(body)[:1200],
                        site=host or site_name,
                        publish_time=clean_scraped_text(str(obj.get("date") or "")),
                        source_type=f"元宝搜索 · {site_name}" if site_name else "元宝搜索返回",
                        trust_level=_grade_trust(host),
                    )
                )
                continue

            # 兼容纯文本页
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if not lines:
                continue
            url_match = re.search(r"https?://[^\s\"'<>]+", text)
            url = url_match.group(0) if url_match else ""
            if is_mirror_url(url):
                continue
            host = _host_of(url)
            page_title = normalize_hit_text(lines[0])
            if is_english_title(page_title):
                continue
            hits.append(
                SearchHit(
                    title=page_title[:120],
                    url=url,
                    snippet=normalize_hit_text(" ".join(lines[1:]))[:600],
                    site=host,
                    source_type="元宝搜索返回",
                    trust_level=_grade_trust(host),
                )
            )
        if hits:
            return hits

    # 形态 1 / 3：递归找文档列表
    for doc in _deep_find_doc_list(payload):
        url = _pick(doc, _URL_KEYS)
        title = _pick(doc, _TITLE_KEYS) or (url[:80] if url else "")
        snippet = _pick(doc, _SNIPPET_KEYS)
        if not (url or title):
            continue
        site = ""
        if url:
            match = re.match(r"https?://([^/]+)", url)
            site = match.group(1) if match else ""
        hits.append(
            SearchHit(
                title=title[:160],
                url=url,
                snippet=snippet[:600],
                site=site,
                publish_time=_pick(doc, _DATE_KEYS),
                source_type="元宝搜索返回",
                trust_level="待评级",
            )
        )

    # 去重（按 URL）
    seen = set()
    unique: List[SearchHit] = []
    for hit in hits:
        key = hit.url or hit.title
        if key in seen:
            continue
        seen.add(key)
        unique.append(hit)

    # 排序：政府站点优先、含中文的优先；同组内保持引擎原始次序（稳定排序）
    def _rank(hit: SearchHit) -> tuple:
        gov = 0 if (hit.site.endswith(".gov.cn") or hit.site in {"gov.cn", "www.gov.cn"}) else 1
        cjk = 0 if re.search(r"[\u4e00-\u9fa5]", hit.title) else 1
        return (gov, cjk)

    unique.sort(key=_rank)
    return unique


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class YuanbaoSearchClient:
    """元宝搜索统一客户端：真实三形态 + Mock 无缝降级。"""

    def __init__(self, config: Optional[Settings] = None) -> None:
        self.settings = config or default_settings
        self.mock = get_mock_index()
        self._client: Optional[httpx.AsyncClient] = None

    # ---------------- 生命周期 ----------------
    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.http_timeout, connect=min(6.0, self.settings.http_timeout)),
                follow_redirects=True,
                trust_env=self.settings.trust_env,
                headers={"User-Agent": "PolicyValidityDetector/1.0 (+yuanbao-search)"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------------- 对外主入口 ----------------
    async def search_channel(
        self,
        spec: QuerySpec,
        *,
        mock_case: Optional[Dict[str, Any]] = None,
        meta: Optional[Dict[str, Any]] = None,
        force_mock: bool = False,
    ) -> ChannelSearchResult:
        """
        执行单条检索通道。

        force_mock=True 时跳过真实接口（演示模式），但仍走同一套结果结构，
        保证前端渲染路径完全一致。
        """
        started = time.perf_counter()

        if force_mock or not self.settings.is_real_api:
            result = await self._mock_result(spec, mock_case, meta)
        else:
            try:
                hits = await self._real_search(spec.query)
                hits = self._filter_hits(hits, meta)
                if hits:
                    result = ChannelSearchResult(
                        step=spec.step,
                        channel=spec.channel,
                        channel_key=spec.channel_key,
                        query=spec.query,
                        purpose=spec.purpose,
                        hit_count=len(hits),
                        key_finding=self._summarize_hits(spec, hits),
                        hits=hits,
                        provider=self.settings.resolved_provider,
                    )
                else:
                    # 真实接口有返回、但全部被净网/属地规则剔除：
                    # 如实返回空通道，绝不用本地仿真数据顶替真实结果，
                    # 否则会对用户形成"查到了"的错觉。
                    result = ChannelSearchResult(
                        step=spec.step,
                        channel=spec.channel,
                        channel_key=spec.channel_key,
                        query=spec.query,
                        purpose=spec.purpose,
                        hit_count=0,
                        key_finding="真实检索命中结果均因跨省 / 非官方来源被剔除，本通道未取得可采信证据。",
                        hits=[],
                        provider=self.settings.resolved_provider,
                    )
                    result.error = "空结果：已剔除跨省与非官方来源"
            except Exception as exc:  # noqa: BLE001 —— 降级必须捕获全部异常
                result = await self._mock_result(spec, mock_case, meta)
                result.degraded = True
                result.error = f"{type(exc).__name__}: {exc}"[:200]

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    # ---------------- 连通性探测 ----------------
    async def probe(self, query: str = "国务院 现行有效 政策 通知") -> Dict[str, Any]:
        """
        用当前配置发起一次真实的轻量检索，专供「测试接口连通性」使用。

        与 `search_channel` 的区别：
        - 不走 Mock 降级：失败就是失败，必须把原因如实告诉用户；
        - 不做属地/净网过滤：只验证「能不能通、鉴权过不过」；
        - 不抛异常：统一返回结构化结果，便于前端直接渲染提示条。
        """
        provider = self.settings.resolved_provider

        # 配置层面的硬伤优先报出来：这类问题重试多少次都一样，
        # 给「网络错误」只会让用户去排查网络。
        if getattr(self.settings, "search_config_hint", "") == "tencent_requires_pair":
            return {
                "ok": False,
                "provider": "tencent_cloud",
                "latency_ms": 0,
                "message": (
                    "这个地址是腾讯云的签名端点（wsa.tencentcloudapi.com），只认 SecretId|SecretKey 成对密钥。"
                    "如果你用的是控制台「服务 API KEY」创建的那把密钥，请把 Base URL 留空"
                    "（或改成 https://api.wsa.cloud.tencent.com），只填密钥即可。"
                ),
            }

        if provider == "none":
            return {
                "ok": False,
                "provider": provider,
                "latency_ms": 0,
                "message": "尚未配置任何搜索凭证：请填写 API Key，或在 .env 中配置 SecretId/SecretKey。",
            }
        if provider == "generic" and "example.com" in (self.settings.yuanbao_api_base or ""):
            return {
                "ok": False,
                "provider": provider,
                "latency_ms": 0,
                "message": "尚未填写搜索接口 Base URL：Bearer 模式必须提供真实网关地址（如 https://your-gateway.com/v1）。",
            }

        started = time.perf_counter()
        try:
            # 探测只打一次，不走 _real_search 的重试链路：
            # 连通性测试要的是"现在通不通"，重试三次只会让用户多等十几秒。
            if provider == "tencent_cloud":
                hits = await self._search_tencent(query)
            elif provider == "apikey":
                hits = await self._search_apikey(query)
            elif provider == "generic":
                hits = await self._search_generic(query)
            else:
                hits = await self._search_mcp(query)
            latency = int((time.perf_counter() - started) * 1000)
            if hits:
                return {
                    "ok": True,
                    "provider": provider,
                    "latency_ms": latency,
                    "hit_count": len(hits),
                    "message": f"搜索接口鉴权通过，实测返回 {len(hits)} 条结果。",
                }
            return {
                "ok": True,
                "provider": provider,
                "latency_ms": latency,
                "hit_count": 0,
                "message": "搜索接口鉴权通过（HTTP 200），但本次探测未解析到结果，请确认接口返回结构。",
            }
        except Exception as exc:  # noqa: BLE001 —— 探测失败必须降级为可读提示
            latency = int((time.perf_counter() - started) * 1000)
            return {
                "ok": False,
                "provider": provider,
                "latency_ms": latency,
                "message": humanize_http_error(exc),
            }

    # ---------------- Mock 路径 ----------------
    async def _mock_result(
        self,
        spec: QuerySpec,
        mock_case: Optional[Dict[str, Any]],
        meta: Optional[Dict[str, Any]],
    ) -> ChannelSearchResult:
        # 轻微随机延迟，让前端的"思考链"动画有真实的节奏感
        await asyncio.sleep(random.uniform(0.15, 0.38))

        if mock_case:
            hits, finding, hit_count = self.mock.hits_for(mock_case, spec.channel_key)
            if hits:
                return ChannelSearchResult(
                    step=spec.step,
                    channel=spec.channel,
                    channel_key=spec.channel_key,
                    query=spec.query,
                    purpose=spec.purpose,
                    hit_count=max(hit_count, len(hits)),
                    key_finding=finding,
                    hits=hits,
                    provider="mock",
                )

        region_domain = (meta or {}).get("gov_domain", "")
        hits, finding = self.mock.synthetic_hits(spec.query, spec.channel_key, region_domain, meta)
        return ChannelSearchResult(
            step=spec.step,
            channel=spec.channel,
            channel_key=spec.channel_key,
            query=spec.query,
            purpose=spec.purpose,
            hit_count=len(hits),
            key_finding=finding,
            hits=hits,
            provider="mock",
        )

    # ---------------- 结果净化 ----------------
    @staticmethod
    def _filter_hits(
        hits: Sequence[SearchHit], meta: Optional[Dict[str, Any]] = None
    ) -> List[SearchHit]:
        """
        真实结果的二次净化：

        1. 论坛 / 问答 / 聚合站一律丢弃；
        2. 地方政策核验执行「属地白名单 + 外省黑名单」双重校验，
           查湖南时混入的海南交警通告、广东转载一律剔除；
        3. 检索层级为国务院时，优先保留中央部委主站，区县机械转载在去重阶段被合并；
        4. 同名文件只留最权威的一条（normalize_title 去重）。
        """
        cleaned = [hit for hit in hits if not is_noise_host(hit.site)]
        cleaned = filter_by_region(cleaned, meta)

        admin = str((meta or {}).get("admin_level", "") or "")
        if admin.startswith("国务院"):
            # 中央文件场景：保留中央部委主站、各级政府站点与官方政务新媒体，
            # 纯商业站点丢弃；同一文件的区县转载由后续标题去重合并为 1 条。
            official = [
                hit for hit in cleaned
                if host_rank(hit.site) <= 3 or _LOCAL_RULE_PAT.search(hit.title or "")
            ]
            if official:
                cleaned = official

        cleaned = dedupe_hits(cleaned)
        # 权威度优先排序（同权威等级保持引擎原序）
        cleaned.sort(key=lambda hit: host_rank(hit.site))
        return cleaned

    # ---------------- 真实路径 ----------------
    async def _real_search(self, query: str) -> List[SearchHit]:
        provider = self.settings.resolved_provider
        attempts = self.settings.http_retries + 1
        last_error: Optional[Exception] = None

        for attempt in range(attempts):
            try:
                if provider == "tencent_cloud":
                    return await self._search_tencent(query)
                if provider == "apikey":
                    return await self._search_apikey(query)
                if provider == "generic":
                    return await self._search_generic(query)
                if provider == "mcp":
                    return await self._search_mcp(query)
                raise RuntimeError(f"未支持的 provider: {provider}")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(0.4 * (2 ** attempt))

        raise last_error or RuntimeError("真实检索失败")

    async def _search_tencent(self, query: str) -> List[SearchHit]:
        s = self.settings
        # Cnt 的合法下界是 10：传 1~9 会返回 InvalidParameter: illegal Cnt
        payload_dict: Dict[str, Any] = {
            "Query": query,
            "Cnt": max(10, min(int(s.search_top_k), 50)),
        }
        payload = json.dumps(payload_dict, ensure_ascii=False, separators=(",", ":"))
        headers = build_tc3_headers(
            secret_id=s.tc_secret_id,
            secret_key=s.tc_secret_key,
            host=s.tc_endpoint,
            service="wsa",
            action=s.tc_action,
            version=s.tc_version,
            region=s.tc_region,
            payload=payload,
        )
        response = await self._http().post(
            f"https://{s.tc_endpoint}", content=payload.encode("utf-8"), headers=headers
        )
        response.raise_for_status()
        data = response.json()
        error = (data.get("Response") or {}).get("Error")
        if error:
            raise RuntimeError(f"腾讯云返回错误：{error.get('Code')} {error.get('Message')}")
        return parse_payload_to_hits(data)

    async def _search_apikey(self, query: str) -> List[SearchHit]:
        """
        元宝搜索「服务 API KEY 兼容模式」。

        官方口径：POST https://api.wsa.cloud.tencent.com/SearchPro
        鉴权：Authorization: Bearer <服务密钥>
        请求体与 TC3 完全一致（Action / Version / Query / Cnt），
        响应同样是 Response.Pages（JSON 字符串数组），因此解析逻辑完全复用。

        这是当前对用户最省事的一条路径：只填一把 Key，域名用默认值即可。
        """
        s = self.settings
        url = f"{(s.apikey_base or 'https://api.wsa.cloud.tencent.com').rstrip('/')}{s.apikey_path or '/SearchPro'}"
        payload: Dict[str, Any] = {
            "Action": "SearchPro",
            "Version": s.tc_version or "2025-05-08",
            "Query": query,
            "Cnt": max(10, min(int(s.search_top_k), 50)),
        }
        headers = {
            "Authorization": f"Bearer {s.yuanbao_api_key}",
            "Content-Type": "application/json; charset=utf-8",
        }
        response = await self._http().post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        error = (data.get("Response") or {}).get("Error")
        if error:
            code = error.get("Code") or ""
            message = error.get("Message") or ""
            if "Auth" in code or "Unauthorized" in code:
                raise RuntimeError(f"服务 API KEY 鉴权失败：{code} {message}")
            raise RuntimeError(f"元宝搜索返回错误：{code} {message}")
        return parse_payload_to_hits(data)

    async def _search_generic(self, query: str) -> List[SearchHit]:
        s = self.settings
        url = f"{s.yuanbao_api_base.rstrip('/')}{s.yuanbao_search_path}"
        body = {s.yuanbao_query_field: query, "top_k": s.search_top_k, "count": s.search_top_k}
        headers = {
            "Authorization": f"Bearer {s.yuanbao_api_key}",
            "Content-Type": "application/json",
        }
        response = await self._http().post(url, json=body, headers=headers)
        response.raise_for_status()
        return parse_payload_to_hits(response.json())

    async def _search_mcp(self, query: str) -> List[SearchHit]:
        s = self.settings
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 1_000_000,
            "method": "tools/call",
            "params": {
                "name": s.mcp_tool,
                "arguments": {"query": query, "count": s.search_top_k, "top_k": s.search_top_k},
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if s.mcp_token:
            headers["Authorization"] = f"Bearer {s.mcp_token}"

        response = await self._http().post(s.mcp_url, json=payload, headers=headers)
        response.raise_for_status()

        # Streamable HTTP 可能返回 SSE 帧
        text = response.text.strip()
        data: Any
        if text.startswith("event:") or text.startswith("data:"):
            last_data = ""
            for line in text.splitlines():
                if line.startswith("data:"):
                    last_data = line[5:].strip()
            data = json.loads(last_data) if last_data else {}
        else:
            data = response.json()

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"MCP 错误：{data['error']}")

        # MCP 结果通常包装在 result.content[*].text 里
        result = data.get("result", data) if isinstance(data, dict) else data
        if isinstance(result, dict):
            contents = result.get("content")
            if isinstance(contents, list):
                for item in contents:
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw = item.get("text", "")
                        try:
                            return parse_payload_to_hits(json.loads(raw))
                        except json.JSONDecodeError:
                            return parse_payload_to_hits([raw])
        return parse_payload_to_hits(data)

    # ---------------- 命中摘要 ----------------
    @staticmethod
    def _summarize_hits(spec: QuerySpec, hits: List[SearchHit]) -> str:
        """真实模式下，用规则给出一条可读的关键发现（不调用大模型）。"""
        if not hits:
            return "未检索到相关结果。"
        top = hits[0]
        snippet = re.sub(r"\s+", " ", top.snippet)[:120]
        return f"命中 {len(hits)} 条，首条来自 {top.site or '未知站点'}：{top.title[:60]}｜{snippet}"
