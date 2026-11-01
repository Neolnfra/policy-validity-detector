"""
core/query_builder.py —— 政策要素解析器 + 三路检索词裂变器
================================================================================

本模块是整个 Agent 的"翻译层"：把用户口语化的提问，翻译成政务检索专家才会写的
三组专业 Query。它必须是确定性的（deterministic）——同样输入永远得到同样检索词，
这样评委复现你的截图时才不会"结果对不上"。

三路互补的检索策略
------------------
    通道 A  原文溯源     → 解决"这份文件到底怎么写的、有效期到哪天"
    通道 B  废止排查     → 解决"这份文件有没有被提前废掉"（最容易被忽略、也最致命）
    通道 C  新政替代     → 解决"现在按什么办"（用户真正想要的答案）

为什么必须三路
--------------
只做通道 A，你会得到一份"看起来仍然有效"的原文，因为官网原文不会自己标注失效；
只做通道 B，你只能证明"被废止"，却说不出"现在该怎么办"；
只做通道 C，你无法确认当前援引的到底是哪一版。三路交叉，才能得出可核验的结论。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.region_router import Region, RegionMatch, build_site_expression

# ---------------------------------------------------------------------------
# 6.1 三路搜索词裂变 Prompt（真实模式下交给大模型改写检索词时使用）
# ---------------------------------------------------------------------------
QUERY_BUILDER_PROMPT = """你是一名精通中国各级政府公文体系与政务检索的专家 Agent。
用户输入了一段政策信息：【{raw_input}】，指定或识别的地域为：【{region}】（对应政务网域名：{gov_domain}）。

请为“元宝搜索 API”构造 3 组互补的专业检索 Query，以彻底查明该政策现在的真实法律效力：
1. [channel_origin] 原文与有效期核验词：结合 site:{gov_domain}、政策全称或发文字号，精准定位原文及文末“附则/有效期”条款（注意：行政规范性文件有效期通常为 3 年或 5 年，标注“试行/暂行”的通常为 2 年）。
2. [channel_abolish] 废止与清理目录排查词：构造“政策名/发文字号 + 废止 / 宣布失效 / 行政规范性文件清理结果 / 决定修改”等关键词，排查该政策是否被提前废止。
3. [channel_replacement] 最新替代新政与官方解读词：构造同地区、同业务领域的最新年份（{year} / {next_year}）政策词及官方微信公众号“一图读懂/政策解读”检索词。

请严格以 JSON 格式输出这 3 条检索指令，字段为：
{{"queries": [{{"channel": "origin|abolish|replacement", "query": "检索式", "purpose": "这一路要验证什么"}}]}}
只输出 JSON，不要任何解释性文字。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class PolicyMeta:
    """从用户输入中解析出的政策结构化元数据。"""

    raw_input: str
    title: str = ""
    doc_number: str = ""
    region: str = ""
    region_key: str = ""
    admin_level: str = ""
    department: str = ""
    publish_date: str = ""
    stated_expiry: str = ""
    doc_year: int = 0
    is_trial: bool = False
    topic: str = ""
    gov_domain: str = ""
    site_filter: str = ""
    region_confidence: float = 0.0
    matched_by: str = ""
    date_source: str = "unknown"  # explicit | derived | unknown
    expiry_note: str = ""
    # 口语/短词场景：输入不是公文名，需先做术语映射，再从检索结果锁定焦点政策
    is_short_query: bool = False
    search_terms: List[str] = field(default_factory=list)
    policy_category: str = "generic"  # subsidy | regulation | generic
    locked_policy_title: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "doc_number": self.doc_number,
            "region": self.region,
            "region_key": self.region_key,
            "admin_level": self.admin_level,
            "department": self.department,
            "publish_date": self.publish_date,
            "stated_expiry": self.stated_expiry,
            "doc_year": self.doc_year,
            "is_trial": self.is_trial,
            "topic": self.topic,
            "gov_domain": self.gov_domain,
            "site_filter": self.site_filter,
            "region_confidence": round(self.region_confidence, 2),
            "matched_by": self.matched_by,
            "date_source": self.date_source,
            "expiry_note": self.expiry_note,
            "is_short_query": self.is_short_query,
            "search_terms": list(self.search_terms),
            "policy_category": self.policy_category,
            "locked_policy_title": self.locked_policy_title,
        }


@dataclass
class QuerySpec:
    """一条检索指令。"""

    step: int
    channel: str
    channel_key: str  # origin | abolish | replacement
    query: str
    purpose: str
    expected: str = ""
    site_filter: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "channel": self.channel,
            "channel_key": self.channel_key,
            "query": self.query,
            "purpose": self.purpose,
            "expected": self.expected,
            "site_filter": self.site_filter,
        }


# ---------------------------------------------------------------------------
# 正则与词表
# ---------------------------------------------------------------------------
_DOC_NUMBER_RE = re.compile(
    r"([\u4e00-\u9fa5]{1,12})\s*[〔\[\(（]\s*((?:19|20)\d{2})\s*[〕\]\)）]\s*(\d{1,4})\s*号"
)
_BOOK_TITLE_RE = re.compile(r"《([^》]{2,80})》")
_DATE_CN_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_ISO_RE = re.compile(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})")
_EXPIRY_CN_RE = re.compile(
    r"有效期(?:限)?\s*(?:至|到|截止(?:到|至)?|为)?\s*((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_EXPIRY_ISO_RE = re.compile(
    r"有效期(?:限)?\s*(?:至|到|截止(?:到|至)?|为)?\s*((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})"
)
_EXPIRY_YEARS_RE = re.compile(r"有效期(?:限)?\s*(?:为)?\s*([1-9]\d?)\s*年")
_PUBLISH_HINT_RE = re.compile(
    r"(?:印发|发布|成文|施行|通过)[^0-9]{0,12}((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
_DEPARTMENT_RE = re.compile(
    r"([\u4e00-\u9fa5]{2,24}?(?:人民政府办公厅|人民政府办公室|人民政府|管理委员会|工作委员会|"
    r"委员会|办公厅|办公室|总局|总署|管理局|监管局|人力资源社会保障局|人力资源和社会保障局|"
    r"发展和改革委员会|工信局|工业和信息化局|财政局|商务局|科技局|教育局|卫生健康委员会|"
    r"医疗保障局|市场监督管理局|税务局|住建局|交通运输局|农业农村局|文化和旅游局|"
    r"局|厅|部|署))"
)

# 标题中可安全剥离的虚词 / 文种词（剥离后才是"业务主题"）
_TITLE_STOP_PHRASES: Tuple[str, ...] = (
    "关于", "印发", "转发", "批转", "进一步", "加快", "推动", "促进", "深入推进",
    "扎实", "做好", "加强", "的实施意见", "的实施办法", "的若干意见", "的若干措施",
    "实施意见", "实施办法", "管理办法", "实施细则", "行动方案", "工作方案",
    "若干措施", "若干意见", "若干政策", "暂行规定", "暂行办法", "试行办法",
    "通知", "意见", "办法", "方案", "细则", "规定", "决定", "公告", "批复",
    "暂行", "试行", "的通知", "的", "等", "有关", "做好", "开展",
)

# 省级全称，用于从标题中剥离属地前缀，避免主题词被地名污染
_REGION_PREFIX_RE = re.compile(
    r"^(?:北京市|天津市|上海市|重庆市|河北省|山西省|辽宁省|吉林省|黑龙江省|江苏省|浙江省|"
    r"安徽省|福建省|江西省|山东省|河南省|湖北省|湖南省|广东省|海南省|四川省|贵州省|云南省|"
    r"陕西省|甘肃省|青海省|台湾省|内蒙古自治区|广西壮族自治区|西藏自治区|宁夏回族自治区|"
    r"新疆维吾尔自治区|香港特别行政区|澳门特别行政区|杭州市|深圳市|广州市|长沙市|成都市|"
    r"南京市|苏州市|武汉市|西安市|郑州市|合肥市|济南市|青岛市|厦门市|福州市|宁波市|东莞市|"
    r"佛山市|无锡市|常州市|南昌市|昆明市|贵阳市|南宁市|海口市|沈阳市|大连市|长春市|"
    r"哈尔滨市|石家庄市|太原市|合肥市|呼和浩特市|兰州市|西宁市|银川市|乌鲁木齐市|拉萨市)"
)

# ---------------------------------------------------------------------------
# 公文文种词：出现任意一个，说明用户输入的是"公文名"而不是口语词
# ---------------------------------------------------------------------------
_POLICY_DOC_WORDS: Tuple[str, ...] = (
    "办法", "条例", "通知", "意见", "细则", "规定", "方案", "决定", "公告",
    "规划", "规则", "标准", "指南", "批复", "复函", "实施", "暂行", "试行",
)

# ---------------------------------------------------------------------------
# 口语 / 行业简称 → 法定公文术语映射
# ---------------------------------------------------------------------------
# 为什么必须有这张表
# ----------------
# 政务网上不会有人写「电瓶车管理办法」，只会写「电动自行车管理办法」。
# 用户输入口语词时若直接拿原词去检索，命中率极低，还会把「电瓶车」当成公文名，
# 拼出《电瓶车》这种不存在的文号，最终只能判「存疑」——这是典型的伪不确定性。
COLLOQUIAL_TERMS: Dict[str, Tuple[str, ...]] = {
    "电瓶车": ("电动自行车", "电动两轮车"),
    "电动车": ("电动自行车", "电动两轮车", "新能源汽车"),
    "小电驴": ("电动自行车",),
    "老年代步车": ("低速电动车", "电动三轮车", "四轮低速电动车"),
    "电动三轮": ("电动三轮车", "低速电动车"),
    "生孩子补贴": ("生育补贴", "育儿补贴", "生育津贴"),
    "生娃补贴": ("生育补贴", "育儿补贴", "生育津贴"),
    "生育津贴": ("生育津贴", "生育保险"),
    "买房补贴": ("购房补贴", "安居补助", "人才安居"),
    "租房补贴": ("住房租赁补贴", "租房补助", "安居补助"),
    "汽车补贴": ("汽车以旧换新", "新能源汽车购置补贴", "报废更新补贴"),
    "以旧换新": ("消费品以旧换新", "大规模设备更新"),
    "家电补贴": ("家电以旧换新", "绿色智能家电消费补贴"),
    "消费券": ("消费券", "促消费专项资金"),
    "创业补贴": ("创业担保贷款", "创业补贴", "一次性创业资助"),
    "社保补贴": ("社会保险补贴", "灵活就业社保补贴"),
    "人才补贴": ("人才安居", "人才补贴", "高层次人才"),
    "高新企业": ("高新技术企业", "高企认定"),
    "专精特新": ("专精特新中小企业", "专精特新“小巨人”"),
    "首台套": ("首台（套）重大技术装备", "首台套保险补偿"),
    "充电桩": ("充电基础设施", "充换电设施"),
}

# 政策类型判定词表：决定模块 05 用哪一套对比维度
_SUBSIDY_WORDS: Tuple[str, ...] = (
    "补贴", "补助", "资助", "奖励", "专项资金", "扶持", "以旧换新",
    "消费券", "减免", "退税", "贴息", "经费", "奖补", "保险补偿",
)
_REGULATION_WORDS: Tuple[str, ...] = (
    "管理", "条例", "规定", "登记", "通行", "监管", "整治", "标准",
    "安全", "规范", "许可", "处罚", "认定", "办法",
)

# 焦点政策锁定时的降权词：这些是解读/新闻页，不是规范性文件本体
_FOCUS_PENALTY_WORDS: Tuple[str, ...] = (
    "解读", "问答", "一图读懂", "图解", "新闻", "访谈", "视频", "科普",
    "提示", "曝光", "事故", "整治行动", "政策吹风", "发布会", "答记者问",
    "征求意见", "草案", "转载", "索引",
)

# 公文文种结尾：标题以这些词收尾，才是"文件本体"而不是新闻报道
_DOC_TAIL_WORDS: Tuple[str, ...] = (
    "办法", "条例", "规定", "细则", "通知", "意见", "方案", "决定",
    "公告", "规划", "规则", "标准", "指南", "批复", "复函", "通告",
)

# 新闻标题特征词：出现即大幅降权
_NEWS_HINTS: Tuple[str, ...] = (
    "都能", "来了", "速看", "收藏", "宝妈", "最新", "公布", "注意", "重磅",
)


def is_short_query(text: str, doc_number: str = "") -> bool:
    """
    判断输入是否为「口语词 / 短主题词」而非公文名。

    判定条件（全部满足才算）：
        1. 没有发文字号；
        2. 没有书名号；
        3. 不含任何公文文种词（办法/条例/通知/意见/细则/规定/方案…）；
        4. 去掉标点后不超过 14 个字符。
    """
    core = _clean(text)
    if doc_number or "《" in core:
        return False
    if any(word in core for word in _POLICY_DOC_WORDS):
        return False
    letters = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", core)
    return 0 < len(letters) <= 14


def expand_search_terms(topic: str) -> List[str]:
    """
    把口语主题词扩展为政务网真正会用的法定术语。

    命中映射表时只返回法定术语（不再保留口语词，因为官网上没有这种写法）；
    未命中时原样返回清洗后的主题词，保证任意输入都有检索词可用。
    """
    core = topic or ""
    for _ in range(2):
        core = _REGION_PREFIX_RE.sub("", core)
    core = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", core)

    for key, values in COLLOQUIAL_TERMS.items():
        if key in core or (len(core) >= 2 and core in key):
            return list(values)[:3]

    return [core] if core else []


def detect_policy_category(*texts: str) -> str:
    """
    判定政策类型，供模块 05 选择对比维度。

    subsidy    → 资金补贴 / 专项申报类
    regulation → 行政管理 / 交通规范 / 行业监管类
    generic    → 通用
    """
    blob = " ".join(t for t in texts if t)
    sub = sum(1 for word in _SUBSIDY_WORDS if word in blob)
    reg = sum(1 for word in _REGULATION_WORDS if word in blob)
    if sub and sub >= reg:
        return "subsidy"
    if reg:
        return "regulation"
    return "generic"



# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    return re.sub(r"[\s\u3000\u200b\ufeff]+", " ", (text or "")).strip()


def _fmt_date(year: Any, month: Any, day: Any) -> str:
    try:
        y, m, d = int(year), int(month), int(day)
        return date(y, m, d).isoformat()
    except (TypeError, ValueError):
        return ""


def _safe_date(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _extract_topic(title: str) -> str:
    """从政策标题中提炼业务主题词，用于"替代新政"通道检索。"""
    if not title:
        return ""
    # 属地前缀可能叠加（吉林省长春市…），最多剥两层
    topic = title.strip()
    for _ in range(2):
        topic = _REGION_PREFIX_RE.sub("", topic)
    topic = re.sub(r"^[^\u4e00-\u9fa5]+", "", topic)
    # 剥掉紧跟在属地后的机关名，如「人民政府关于…」
    topic = re.sub(
        r"^(?:人民政府|政府|人民政府办公厅|办公厅|办公室|委员会|管理委员会)[关于]*",
        "",
        topic,
    )
    # 先按括号切断：「首台（套）重大技术装备…」只需要「首台」之前的业务名
    topic = re.split(r"[（(【\[]", topic, maxsplit=1)[0]
    for phrase in _TITLE_STOP_PHRASES:
        topic = topic.replace(phrase, "")
    topic = re.sub(r"[《》【】（）()、，,。；;：:\"“”]", "", topic)
    topic = re.sub(r"(?:扶持|支持|促进|鼓励|奖励)?政策$", "", topic)
    topic = topic.strip()
    if not topic:
        # 全部被剥离时退回原标题前 16 字
        topic = title[:16]
    return topic[:16]


def _guess_department(text: str, region: Region, doc_number: str) -> str:
    """推断发文机关。优先取文本中显式出现的机关全称。"""
    match = _DEPARTMENT_RE.search(text)
    if match:
        candidate = match.group(1)
        # 过滤掉明显不是机关名的片段
        if len(candidate) >= 4 and not candidate.startswith("根据"):
            return candidate

    # 退而求其次：用发文字号代字 + 属地进行组合推断
    if doc_number:
        prefix = doc_number[:2]
        if "办" in prefix:
            return f"{region.name}人民政府办公厅"
        if region.tier == "national":
            return "国务院"
    if region.tier == "national":
        return "国务院"
    if region.tier == "province":
        return f"{region.name}人民政府"
    return f"{region.name}人民政府"


# ---------------------------------------------------------------------------
# 主解析函数
# ---------------------------------------------------------------------------
def parse_policy_elements(raw_input: str, region_match: RegionMatch) -> PolicyMeta:
    """从任意文本中解析政策要素。全部基于确定性规则，不依赖大模型。"""
    text = _clean(raw_input)
    region = region_match.region

    meta = PolicyMeta(
        raw_input=text,
        region=region.name,
        region_key=region.key,
        admin_level=region.admin_level,
        gov_domain=region.gov_domain,
        site_filter=region.site_filter,
        region_confidence=region_match.confidence,
        matched_by=region_match.matched_by,
    )

    # ---- 1. 发文字号 ----
    doc_match = _DOC_NUMBER_RE.search(text)
    if doc_match:
        org, year, seq = doc_match.group(1), doc_match.group(2), doc_match.group(3)
        meta.doc_number = f"{org}〔{year}〕{seq}号"
        meta.doc_year = int(year)

    # ---- 2. 政策标题 ----
    if is_short_query(text, meta.doc_number):
        # 口语词 / 行业简称：绝不能拼成《电瓶车》这种不存在的公文名。
        # 此处只记录主题词并展开法定术语，真正的政策标题由检索结果锁定
        # （见 resolve_focus_policy）。
        meta.is_short_query = True
        meta.topic = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", _REGION_PREFIX_RE.sub("", text))
        meta.search_terms = expand_search_terms(meta.topic)
        meta.title = ""
        meta.policy_category = detect_policy_category(meta.topic, " ".join(meta.search_terms))
    else:
        title_match = _BOOK_TITLE_RE.search(text)
        if title_match:
            meta.title = title_match.group(1).strip()
        else:
            # 无书名号时：先摘掉发文字号括号，再按中文标点截断。
            # 不能简单地在第一个「（」处切分，否则「首台（套）」这类标题会被腰斩。
            head = _DOC_NUMBER_RE.sub("", text)
            head = re.sub(r"[（(]\s*[）)]", "", head)
            head = re.split(r"[，,。；;？?！!、]", head, maxsplit=1)[0]
            head = _clean(head)[:60]
            meta.title = head or text[:60]

    # ---- 3. 有效期 ----
    expiry = None
    for pattern in (_EXPIRY_CN_RE, _EXPIRY_ISO_RE):
        found = pattern.search(text)
        if found:
            expiry = _fmt_date(*found.groups())
            if expiry:
                break

    expired_years: Optional[int] = None
    years_match = _EXPIRY_YEARS_RE.search(text)
    if years_match:
        try:
            expired_years = int(years_match.group(1))
        except ValueError:
            expired_years = None

    # ---- 4. 发布 / 施行日期 ----
    publish = ""
    hint_match = _PUBLISH_HINT_RE.search(text)
    if hint_match:
        publish = _fmt_date(*hint_match.groups())

    all_dates: List[Tuple[str, str]] = []
    for pattern in (_DATE_CN_RE, _DATE_ISO_RE):
        for found in pattern.finditer(text):
            value = _fmt_date(*found.groups())
            if value:
                all_dates.append((value, found.group(0)))

    if not publish and all_dates:
        # 排除已被判定为有效期的那个日期，取最早的其余日期作为印发日
        candidates = [d for d, _ in all_dates if d != expiry]
        if candidates:
            publish = sorted(candidates)[0]

    # 注意：文号里的年份只能说明"这是哪一年的文件"，绝不能当作印发日期。
    # 本工程宁可留空，也不把 doc_year + 01-01 伪装成精确日期——那本身就是一种幻觉。
    meta.publish_date = publish
    meta.date_source = "explicit" if publish else "unknown"

    # "有效期 5 年" + 明确的印发日 → 可以合规推算失效日（这是算术，不是编造）
    if not expiry and expired_years and publish:
        base = _safe_date(publish)
        if base:
            try:
                expiry = base.replace(year=base.year + expired_years).isoformat()
            except ValueError:
                expiry = f"{base.year + expired_years}-12-31"
            meta.expiry_note = f"依据“有效期 {expired_years} 年”与印发日推算"

    meta.stated_expiry = expiry or ""

    # ---- 5. 试行 / 暂行 判定（这类文件默认生命周期更短）----
    meta.is_trial = any(k in text or k in meta.title for k in ("试行", "暂行", "试点"))
    if meta.is_trial and not meta.stated_expiry:
        # 只写"待核验线索"，不写进 stated_expiry，避免把推算值当事实
        anchor = meta.publish_date or (str(meta.doc_year) if meta.doc_year else "印发")
        meta.expiry_note = (
            f"试行/暂行类文件通常以 2 年为试行期（自{anchor}起算），"
            "须以原文附则确认，本项未计入判定依据"
        )

    # ---- 6. 主题词与发文机关 ----
    if not meta.is_short_query:
        meta.topic = _extract_topic(meta.title)
        meta.policy_category = detect_policy_category(meta.title, meta.raw_input)
    meta.department = _guess_department(text, region, meta.doc_number)

    return meta


# ---------------------------------------------------------------------------
# 三路检索词构造
# ---------------------------------------------------------------------------
def build_queries(meta: PolicyMeta, region_match: RegionMatch, top_k: int = 3) -> List[QuerySpec]:
    """构造三路互补检索式。每一条都可直接丢给元宝搜索 / 通用搜索引擎。"""
    region = region_match.region
    site = build_site_expression(region)
    title = meta.title or meta.raw_input[:30]
    doc = meta.doc_number
    topic = meta.topic or title[:12]
    today = date.today()

    years = f"{today.year - 1} {today.year} {today.year + 1}"
    site_prefix = f"site:{region.gov_domain} " if region.gov_domain else ""
    region_scope = region.name.split("（")[0]

    if meta.is_short_query:
        # 口语词场景：用映射后的法定术语做 OR 组，绝不把口语词当公文名。
        terms = meta.search_terms or [meta.topic or meta.raw_input[:10]]
        if len(terms) > 1:
            subject = "(" + " OR ".join(terms) + ")"
        else:
            subject = f'"{terms[0]}"'

        qa = f"{site_prefix}{subject} (管理办法 OR 条例 OR 规定 OR 实施细则) 全文 施行 有效期"
        qb = f"{site_prefix}{subject} (废止 OR 宣布失效 OR 清理结果 OR 修订)"
        qc = (
            f"{region_scope} {terms[0]} 最新政策 {years} "
            f"{'新国标 过渡期 ' if meta.policy_category == 'regulation' else ''}官方解读 一图读懂"
        )
    else:
        # 通道 A：原文 + 有效期
        # 检索式不要同时塞文号和完整标题：实测「文号 + 标题 + 有效期 附则 全文」
        # 在真实引擎上会召回 0 条政务页，而只保留文号 + 全文/有效期能稳定命中政府公报。
        if doc:
            qa = f'{site_prefix}"{doc}" 全文 有效期'
        else:
            qa = f'{site} "{title}" 全文 施行 有效期'

        # 通道 B：废止 / 清理目录排查
        # 语法要点：
        #   1. site: 必须放最前，否则部分引擎会把它当成普通词；
        #   2. 多个 OR 必须用括号包住，否则 OR 的优先级会让 site: 只作用于最后一个词，
        #      结果漏出论坛帖与外地县区的机械转载。
        if doc:
            qb = f'{site_prefix}"{doc}" (废止 OR 宣布失效 OR 清理结果 OR 修订)'
        else:
            qb = f'{site_prefix}"{title}" (废止 OR 宣布失效 OR 清理结果 OR 修订)'

        # 通道 C：替代新政 + 官微解读（地方核验限定在本级政务网，避免新闻站刷屏）
        if region.tier == "national":
            qc = f'{region_scope} {topic} 最新政策 {years} 官方解读 一图读懂'
        else:
            qc = f'{site_prefix}{topic} 最新政策 {years} 官方解读 一图读懂'

    return [
        QuerySpec(
            step=1,
            channel="政务网原文与有效期核验",
            channel_key="origin",
            query=qa,
            purpose="定位官方原文，核验文末附则中的有效期与施行日期",
            expected="命中 *.gov.cn 原文页，附则含“有效期至 YYYY年MM月DD日”",
            site_filter=region.site_filter,
        ),
        QuerySpec(
            step=2,
            channel="废止与失效目录排查",
            channel_key="abolish",
            query=qb,
            purpose="排查该文件是否被提前废止、宣布失效或列入清理目录",
            expected="命中政府公报的《废止/失效文件目录》或清理结果公告",
            site_filter=region.site_filter,
        ),
        QuerySpec(
            step=3,
            channel="替代新政与官方解读检索",
            channel_key="replacement",
            query=qc,
            purpose="确认当前应当执行的最新政策，以及官方对口径变化的解读",
            expected="命中近两年新政策原文、政策解读或政务新媒体一图读懂",
            site_filter="",
        ),
    ]


def clean_policy_title(title: str) -> str:
    """
    清洗检索结果里的标题，去掉站点后缀与栏目名。

    真实检索返回的标题常常是：
        「湖南省电动自行车管理办法-湖南省人民政府门户网站」
        「国务院关于印发《…》的通知_中国政府网」
    展示成公文名时必须还原为《湖南省电动自行车管理办法》，
    否则会出现"《…-湖南省人民政府门户网站》"这种明显不专业的标题。
    """
    text = re.sub(r"\s+", " ", (title or "").strip())
    if not text:
        return ""

    book = _BOOK_TITLE_RE.search(text)
    if book:
        return book.group(1).strip()

    parts = re.split(r"\s*[-_|｜–—]\s*", text)
    if len(parts) > 1 and len(parts[0]) >= 8:
        tail = " ".join(parts[1:])
        if any(word in tail for word in ("网站", "门户", "频道", "新闻", "政务", "信息公开", "政策库", "公报", "媒体")):
            text = parts[0]

    text = re.sub(r"[（(](?:政策原文|原文|全文|解读|图解|附解读)[）)]$", "", text)
    return text.strip()


# 过于宽泛的词根：只用两个字会把"电动三轮车"错配到"新能源汽车"
_GENERIC_STEMS = {
    "电动", "新能源", "住房", "服务", "产业", "管理", "政策",
    "补贴", "补助", "扶持", "支持", "资金", "项目",
}


def _theme_hit(title: str, snippet: str, terms: Sequence[str]) -> bool:
    """
    候选文件是否仍在用户问的这个主题上。

    只看"办法+属地"会锁错文件：查「老年代步车」时，
    《上海市鼓励购买和使用新能源汽车实施办法》同样含"上海市"和"办法"，
    但两者业务完全不同。这里要求术语（或足够具体的词根）在标题/摘要中出现；
    "电动""新能源"这类泛词根不参与匹配，否则会误放行。
    """
    blob = f"{title} {snippet}"
    for term in terms:
        if not term:
            continue
        if term in blob:
            return True
        for size in (2, 3):
            stem = term[:size]
            if len(stem) < 2 or stem in _GENERIC_STEMS:
                continue
            if stem in blob:
                return True
    return False


def _parse_pub_date(value: str) -> str:
    """把 2020/12/21 02:51:09、2020-12-21 这类发布时间统一成 YYYY-MM-DD。"""
    match = re.match(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", (value or "").strip())
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def resolve_focus_policy(meta: PolicyMeta, channels: Sequence[Any]) -> str:
    """
    从检索结果中锁定「焦点主政策」。

    为什么需要这一步
    ----------------
    用户输入「电瓶车」时，被核验的对象不是「电瓶车」这三个字，而是检索命中的
    《湖南省电动自行车管理办法》。若不锁定，判定引擎会拿口语词去做锚定，
    必然得出「证据链未闭合 → 存疑」的伪结论，还会写出「根据《电瓶车》…」的病句。

    打分原则：
        · 第 1 路（原文）权重最高，第 3 路（新政）次之，第 2 路（废止）最低；
        · 文种为办法/条例/规定/细则的加 6 分，通知/意见/方案/决定加 3.5 分；
        · 标题含属地名的加分（防止锁到外省同名文件）；
        · 解读/问答/新闻/提示类页面重罚，确保锁到的是文件本体。
    """
    best_title = ""
    best_score = 0.0
    best_hit: Any = None
    region_name = (meta.region or "").split("（")[0]
    region_short = re.sub(r"(省|市|自治区|特别行政区)$", "", region_name)

    for channel in channels:
        key = str(getattr(channel, "channel_key", "") or "")
        weight = {"origin": 3.0, "replacement": 1.2, "abolish": 0.4}.get(key, 0.0)
        for index, hit in enumerate(list(getattr(channel, "hits", []) or [])[:10]):
            raw_title = str(getattr(hit, "title", "") or "")
            title = clean_policy_title(raw_title)
            if len(title) < 8:
                continue

            # 主题不一致的候选直接排除（例如查老年代步车，却锁到新能源汽车办法）
            theme_terms = [term for term in (meta.search_terms or []) if term]
            if not theme_terms and meta.topic:
                theme_terms = [meta.topic]
            if theme_terms and not _theme_hit(
                title, str(getattr(hit, "snippet", "") or ""), theme_terms
            ):
                continue

            score = weight - index * 0.35
            tail = re.sub(r"[（(][^）)]*[）)]\s*$", "", title).strip()
            if tail.endswith(_DOC_TAIL_WORDS):
                # 以文种收尾 = 文件本体，这是最强的正向信号
                score += 6
            elif any(word in title for word in ("办法", "条例", "规定", "细则")):
                score += 3.5
            elif any(word in title for word in ("通知", "意见", "方案", "决定", "公告")):
                score += 2.5
            if region_short and region_short in title:
                score += 3
            if re.search(r"〔\d{4}〕\d+号", title):
                score += 1.5
            if "《" in raw_title:
                score += 0.5
            if any(word in title for word in _FOCUS_PENALTY_WORDS):
                score -= 6
            if any(word in title for word in _NEWS_HINTS):
                score -= 5
            if any(mark in title for mark in ("、", "！", "？", "|")):
                score -= 3
            if "：" in title and not tail.endswith(_DOC_TAIL_WORDS):
                score -= 2

            if score > best_score:
                best_title, best_score, best_hit = title, score, hit

    if not best_title or best_score < 7:
        return ""

    meta.locked_policy_title = best_title
    meta.title = best_title
    meta.policy_category = detect_policy_category(
        best_title, meta.topic, " ".join(meta.search_terms)
    )

    # 锁定文件的同时把它在政务网上的发布日带回，时间轴才能有真实的起点。
    # 这是"抓取到的日期"，不是推算值，因此单独标记 date_source=retrieved。
    if best_hit is not None and not meta.publish_date:
        published = _parse_pub_date(str(getattr(best_hit, "publish_time", "") or ""))
        if published:
            meta.publish_date = published
            meta.date_source = "retrieved"

    return best_title
