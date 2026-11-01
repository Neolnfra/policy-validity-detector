"""
core/validity_judge.py —— 零幻觉效力判定 + 权威度评分 + 新旧条款 Diff 引擎
================================================================================

判定哲学
--------
"不确定"是一个合法结论，而且往往是最负责任的结论。

本引擎的默认姿态是【怀疑】：
    - 只看到原文，不能判定有效（原文不会自己声明失效）；
    - 只看到"继续有效"，也要核对有效期是否已过；
    - 只有转载源、没有一手信源时，一律降级为 UNCERTAIN，绝不猜测。

四态状态机
----------
    🟢 VALID      现行有效
    🟡 REPLACED   部分失效 / 已被新政替代
    🔴 EXPIRED    已废止 / 到期失效
    ⚪ UNCERTAIN  存疑待人工核验

权威度评分（authority_score）
-----------------------------
    基础分 45
    + S 级信源（政务网原文 / 政府公报 / 清理目录）× 12（最多计 3 条）
    + A 级信源（官方解读 / 官微一图读懂）× 6
    + B 级信源（属地园区通知）× 2
    + 命中 .gov.cn 域名 +10
    - D 级信源（无出处转载）× 8
    上下限收敛到 [20, 99]
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import time
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from config import Settings, settings as default_settings
from core.query_builder import PolicyMeta
from core.region_router import RegionMatch
from core.runtime_config import humanize_http_error
from core.yuanbao_client import (
    ChannelSearchResult,
    SearchHit,
    get_mock_index,
    host_rank,
    normalize_title,
    titles_conflict,
)



# ---------------------------------------------------------------------------
# 状态字典（Mock 数据集未加载时的兜底）
# ---------------------------------------------------------------------------
STATUS_CATALOG: Dict[str, Dict[str, str]] = {
    "VALID": {
        "status_label": "现行有效",
        "status_color": "emerald",
        "emoji": "🟢",
        "description": "已定位官方原文，未超过文件规定的有效期，且未被废止。",
    },
    "REPLACED": {
        "status_label": "部分失效 / 已被新政替代",
        "status_color": "amber",
        "emoji": "🟡",
        "description": "部分条款被后续通知修改，或已有新版接续政策出台。",
    },
    "EXPIRED": {
        "status_label": "已废止 / 到期失效",
        "status_color": "red",
        "emoji": "🔴",
        "description": "已在废止失效目录中列名，或有效期已过且无延期通知。",
    },
    "UNCERTAIN": {
        "status_label": "存疑待人工核验",
        "status_color": "slate",
        "emoji": "⚪",
        "description": "缺乏官方一手信源，或存在执行口径冲突。",
    },
}

_TIMELINE_STATUS_ORDER = [
    "historical", "revised", "expired", "abolished", "current", "uncertain", "conflicting",
]


def _catalog() -> Dict[str, Dict[str, str]]:
    """优先使用 mock 数据集里的状态字典，保证前后端口径一致。"""
    catalog = get_mock_index().status_catalog
    return catalog or STATUS_CATALOG


def _status_meta(code: str) -> Dict[str, str]:
    catalog = _catalog()
    return catalog.get(code, STATUS_CATALOG.get(code, STATUS_CATALOG["UNCERTAIN"]))


# ---------------------------------------------------------------------------
# 大模型客户端（混元 / OpenAI 兼容）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 文本 Diff（短语级，绝不切碎中文词）
# ---------------------------------------------------------------------------
_CLAUSE_DELIMS = "，。；："


def _split_clause_units(text: str) -> List[Tuple[str, str]]:
    """
    把整段文本切成「语义短语 + 尾部标点」单元。

    切分边界只用中文标点（，。；：），不碰任何汉字，因此永远不会出现
    「即可申 / 报」这类把双字词劈成两半的结果。连续标点会合并到前一个单元的尾部。
    """
    units: List[Tuple[str, str]] = []
    buf = ""
    for ch in text:
        if ch in _CLAUSE_DELIMS:
            if buf:
                units.append((buf, ch))
                buf = ""
            elif units:
                units[-1] = (units[-1][0], units[-1][1] + ch)
            else:
                units.append(("", ch))
        else:
            buf += ch
    if buf:
        units.append((buf, ""))
    return units


def build_diff_segments(old: str, new: str, max_segments: int = 400) -> Dict[str, List[Dict[str, str]]]:
    """
    短语级差异对比，输出可渲染成 Git Diff 风格红绿高亮的分段数组。

    为什么不再是字符级
    ------------------
    中文没有空格分词，字符级 SequenceMatcher 会把「申报」切成「申」+「报」，
    高亮后就是「即可申|报」「项目投|产」这种破碎观感。本实现改为：

        1. 以中文标点为边界，切成完整语义短语；
        2. 判断整段短语是否原样出现在另一侧文本中：
              出现   -> equal（不标注）
              未出现 -> delete / insert（整段标注）
        3. 标点单独成段，保证高亮在短语边界干净收尾。

    结果：任何双字词都不会被拆开，也不会引入多余空格。

    返回：
        {"old_segments": [{"op": "equal|delete", "text": "..."}],
         "new_segments": [{"op": "equal|insert", "text": "..."}]}
    """
    if not old and not new:
        return {"old_segments": [], "new_segments": []}

    old_units = _split_clause_units(old)
    new_units = _split_clause_units(new)
    old_flat = "".join(body for body, _ in old_units if body)
    new_flat = "".join(body for body, _ in new_units if body)

    def _build(units: List[Tuple[str, str]], other_flat: str, changed_op: str) -> List[Dict[str, str]]:
        segments: List[Dict[str, str]] = []
        for body, punct in units:
            if body:
                matched = len(body) >= 2 and body in other_flat
                segments.append({"op": "equal" if matched else changed_op, "text": body})
            if punct:
                # 标点始终视为未变更，避免高亮把逗号句号也染色
                segments.append({"op": "equal", "text": punct})
        return segments

    old_segments = _build(old_units, new_flat, "delete")
    new_segments = _build(new_units, old_flat, "insert")

    # 保真校验：拼回原文必须逐字一致，否则退回整段（宁可不高亮，也不能改字）
    if "".join(s["text"] for s in old_segments) != old or "".join(s["text"] for s in new_segments) != new:
        old_segments = [{"op": "delete", "text": old}]
        new_segments = [{"op": "insert", "text": new}]

    # 分段过多时退回整段，避免前端渲染压力（本实现通常只有 4~8 段）
    if len(old_segments) > max_segments or len(new_segments) > max_segments:
        old_segments = [{"op": "delete", "text": old}]
        new_segments = [{"op": "insert", "text": new}]

    return {"old_segments": old_segments, "new_segments": new_segments}


def enrich_clause_diffs(clause_diffs: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给每条条款 Diff 补充短语级分段，供前端做红绿高亮。"""
    enriched: List[Dict[str, Any]] = []
    for diff in clause_diffs:
        item = dict(diff)
        segments = build_diff_segments(
            str(diff.get("old_clause", "")), str(diff.get("new_clause", ""))
        )
        item["old_segments"] = segments["old_segments"]
        item["new_segments"] = segments["new_segments"]
        enriched.append(item)
    return enriched


# ---------------------------------------------------------------------------
# 权威度 / 置信度评分
# ---------------------------------------------------------------------------
_TRUST_WEIGHT = {"S": 12, "A": 6, "B": 2, "C": 0, "D": -8}


def _trust_grade(trust_level: str) -> str:
    if not trust_level:
        return "C"
    match = re.match(r"([SABCD])", trust_level.strip().upper())
    return match.group(1) if match else "C"


def score_authority(channels: Sequence[ChannelSearchResult]) -> int:
    hits = [hit for ch in channels for hit in ch.hits]
    if not hits:
        return 20

    score = 45
    s_count = 0
    gov_bonus = 0

    for hit in hits:
        grade = _trust_grade(hit.trust_level)
        if grade == "S":
            if s_count < 3:
                s_count += 1
                score += _TRUST_WEIGHT["S"]
        else:
            score += _TRUST_WEIGHT.get(grade, 0)

        if gov_bonus == 0 and (
            hit.site.endswith(".gov.cn") or hit.site in {"gov.cn", "www.gov.cn"}
        ):
            gov_bonus = 10

    score += gov_bonus
    return max(20, min(99, score))


def _expiry_passed(stated_expiry: str) -> Optional[bool]:
    """判断文件标注的有效期是否已过。无法解析时返回 None（不猜）。"""
    if not stated_expiry:
        return None
    match = re.search(r"((?:19|20)\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", stated_expiry)
    if not match:
        return None
    try:
        expiry = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return expiry < date.today()


# ---------------------------------------------------------------------------
# 规则引擎判定
# ---------------------------------------------------------------------------
_ABOLISH_TERMS = ("废止", "宣布失效", "决定废止", "予以废止", "已废止", "失效文件目录", "清理结果", "停止执行")
_REPLACE_TERMS = ("接续", "替代", "修订", "补充通知", "新政策", "新版", "扩围", "调整", "重新印发", "新版细则")
_VALID_TERMS = ("继续有效", "现行有效", "尚未废止", "有效期至", "延续有效")

# 「保留 / 继续有效」类措辞：清理决定里出现这些词，说明该文件并未被废止
_RETAIN_TERMS = ("保留", "继续有效", "仍然有效", "未列入", "不在此次", "予以保留", "暂不废止")


def _has_action_term(text: str, terms: Sequence[str], target_title: str = "") -> bool:
    """
    文本里是否出现了"动作性"的废止/修订措辞，且动作指向的是目标文件。

    两类真实误判都在这里拦住：
        1. 「2025年修订版」是版本号，不是"被修订"；
        2. 「新修订的《优质中小企业梯度培育管理办法》」说的是另一份文件
           （工信部办法），不能拿来判定北京市实施细则被修订。
    """
    for term in terms:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            context = text[max(0, index - 14) : index + len(term) + 26]

            if term in {"修订", "修改"} and any(
                mark in context for mark in ("修订版", "修改版", "年修订")
            ):
                start = index + len(term)
                continue

            book = re.search(r"《([^》]{4,80})》", context)
            if book and target_title:
                name = book.group(1).strip()
                if name not in target_title and target_title not in name:
                    # 动作发生在另一份文件上，与本次核验对象无关
                    start = index + len(term)
                    continue
            return True
    return False


def _local_verdict(blob: str, anchors: Sequence[str], target_title: str = "") -> str:
    """
    判断一段文本是否【真的】把目标文件废止了。

    为什么不能只看"废止"二字
    ------------------------
    政府清理决定的原文常常长这样：
        「经清理，决定废止部分省政府规章，保留《湖南省电动自行车管理办法》。」
    如果只看词频，这句话会被当成"该办法被废止"的铁证——恰好判反。

    正确做法：定位目标文件出现的位置，看它前后 40 字内是"废止"还是"保留"。
    """
    for anchor in anchors:
        if not anchor:
            continue
        for match in re.finditer(re.escape(anchor), blob):
            window = blob[max(0, match.start() - 40) : match.end() + 40]
            if any(term in window for term in _RETAIN_TERMS):
                continue
            if _has_action_term(window, _ABOLISH_TERMS, target_title):
                return "abolish"
            if _has_action_term(window, _REPLACE_TERMS, target_title):
                return "replace"
    return ""


def _anchor_strength(meta: PolicyMeta, hits: Sequence[SearchHit]) -> Tuple[int, int, int]:
    """
    计算"指向同一份文件"的证据强度，返回 (锚定命中数, 废止锚定数, 替代锚定数)。

    为什么必须做这一步
    ------------------
    真实检索里，"废止"这个词到处都有：清理工作的背景解读、往期废止目录、
    其它文件的失效公告……如果只看词频就判 EXPIRED，几乎任何政策都会被判死。
    正确做法是：先确认某条命中确实指向【这份文件】（文号出现，或标题 8 字滑窗命中），
    再看同一段文本里是否出现废止/替代措辞。词与文号必须同现，才算铁证。
    """
    doc = (meta.doc_number or "").strip()
    title = (meta.title or "").strip()
    windows: List[str] = []
    if len(title) >= 8:
        windows = [title[i : i + 8] for i in range(0, max(1, len(title) - 7), 2)]

    anchors = [doc, title, *windows]
    anchor_hits = 0
    abolish_anchor = 0
    replace_anchor = 0

    for hit in hits:
        blob = f"{hit.title} {hit.snippet}"
        on_doc = bool(doc) and doc in blob
        on_title = any(w in blob for w in windows)
        if not (on_doc or on_title):
            continue
        anchor_hits += 1
        verdict = _local_verdict(blob, anchors, title)
        if verdict == "abolish":
            abolish_anchor += 1
        elif verdict == "replace":
            replace_anchor += 1

    return anchor_hits, abolish_anchor, replace_anchor


def judge_from_evidence(
    meta: PolicyMeta,
    region_match: RegionMatch,
    channels: Sequence[ChannelSearchResult],
) -> Dict[str, Any]:
    """
    无大模型时的确定性判定。返回结构与 judge_from_case 完全一致，
    因此前端无需区分两种来源。
    """
    hits = [hit for ch in channels for hit in ch.hits]
    corpus = " ".join(f"{hit.title} {hit.snippet}" for hit in hits)

    demo_only = bool(hits) and all(hit.source_type.startswith("演示占位") for hit in hits)
    no_source = not hits

    valid_score = sum(2 for term in _VALID_TERMS if term in corpus)
    expiry_state = _expiry_passed(meta.stated_expiry)
    anchor_hits, abolish_anchor, replace_anchor = _anchor_strength(meta, hits)

    # ---- 状态判定：先看铁证，再看旁证，最后宁可存疑 ----
    locked = bool(getattr(meta, "locked_policy_title", ""))
    if demo_only or no_source:
        status_code = "UNCERTAIN"
        confidence = 50
        reason = "未获得可采信的一手官方信源，按零幻觉原则拒绝推断。"
    elif abolish_anchor >= 1 or expiry_state is True:
        status_code = "EXPIRED"
        confidence = 92 if abolish_anchor >= 1 else 78
        if abolish_anchor >= 1:
            reason = (
                "检索到同时包含该文件标识（文号或标题片段）与废止/失效措辞的官方页面，"
                "可认定其已不具备执行效力。"
            )
        else:
            reason = f"原文标注的有效期（{meta.stated_expiry}）已届满，且未检索到延续公告。"
    elif replace_anchor >= 1:
        status_code = "REPLACED"
        confidence = 84
        reason = "检索到指向该文件的修订、补充或接续政策，原文件部分条款的申报条件与标准已发生变化。"
    elif anchor_hits >= 1 and (locked or valid_score >= 1):
        # 焦点政策已在官方政务网定位到原文，且未检索到任何废止/替代铁证。
        # 这正是「电瓶车 → 湖南省电动自行车管理办法」应当得到的结论：
        # 不能因为用户用词不专业，就把一份现行有效的办法判成存疑。
        status_code = "VALID"
        confidence = 82 if locked else 80
        reason = "已在官方政务网定位到该文件原文，未见废止、宣布失效或替代记录。"
    elif anchor_hits >= 1:
        status_code = "UNCERTAIN"
        confidence = 62
        reason = "已检索到指向该文件的页面，但其中既无有效期条款，也无废止或延续的明确表述，无法据此定性。"
    else:
        status_code = "UNCERTAIN"
        confidence = 55
        reason = "检索结果中未见包含该文号或标题片段的官方页面，证据链未闭合，拒绝给出定性结论。"

    status = _status_meta(status_code)
    authority = score_authority(channels)

    # 主文件仍有效、但检索到同主题的新国标/补充通知时，标签要体现"有效 + 有补充"，
    # 否则用户会以为拿着 2020 年的办法就能直接办 2026 年的事。
    status_label = status["status_label"]
    supplement_terms = ("新国标", "新标准", "强制性标准", "补充通知", "过渡期", "消费提示")
    supplement_found = any(term in corpus for term in supplement_terms)
    if status_code == "VALID" and supplement_found:
        status_label = "现行有效（附最新标准补充）"

    # ---- 结论摘要 ----
    if meta.title:
        doc_desc = f"《{meta.title}》"
        if meta.doc_number:
            doc_desc += f"（{meta.doc_number}）"
    elif meta.is_short_query:
        # 口语词且未锁定到具体文件：绝不写成《电瓶车》
        doc_desc = f"「{meta.topic or meta.raw_input[:10]}」相关现行政策"
    else:
        doc_desc = "目标政策"

    summary_map = {
        "EXPIRED": f"{doc_desc}经核验已不具备执行效力。{reason}现行办理应以最新替代政策为准。",
        "REPLACED": f"{doc_desc}本体仍在，但核心条款已被后续文件修改。{reason}申报前须逐条比对差异。",
        "VALID": f"{doc_desc}经核验现行有效。{reason}执行时仍需以当期申报通知的具体口径为准。",
        "UNCERTAIN": f"{doc_desc}的效力状态无法确认。{reason}建议转人工复核或致电主管部门确认后再办理。",
    }

    # ---- 幻觉对比 ----
    naive = (
        f"根据{doc_desc}，该事项可按原文规定的条件与标准继续申请，文件长期有效；"
        f"只需准备{match_org(meta)}要求的常规材料即可提交，无需另行确认政策是否仍然执行。"
    )
    top_source = hits[0] if hits else None
    verified = (
        f"经元宝搜索三路求证：{reason}"
        + (f"首要依据为 {top_source.site} 的《{top_source.title[:50]}》。" if top_source else "")
        + f"当前结论为「{status_label}」，请勿直接引用原文条款办理。"
    )

    return {
        "validity_result": {
            "status_code": status_code,
            "status_label": status_label,
            "status_color": status["status_color"],
            "status_emoji": status.get("emoji", ""),
            "confidence_score": confidence,
            "authority_score": authority,
            "summary": summary_map[status_code],
            "hallucination_comparison": {
                "naive_llm_answer": naive,
                "yuanbao_verified_answer": verified,
            },
        },
        "timeline": build_timeline(meta, status_code, channels),
        "clause_diffs": enrich_clause_diffs(build_diffs_from_evidence(meta, channels)),
        "sources": build_sources(channels),
    }


def match_org(meta: PolicyMeta) -> str:
    return meta.department or meta.region or "主管部门"


# 事件类型识别：按标题/摘要中的措辞推断这个日期节点发生了什么
_EVENT_RULES: Tuple[Tuple[Tuple[str, ...], str, str], ...] = (
    # 「保留 / 继续有效」优先于「废止」：清理决定常同时出现两个词
    (("保留", "继续有效", "仍然有效"), "规章清理（明确保留）", "current"),
    (("废止", "宣布失效", "失效文件", "停止执行", "予以废止"), "明令废止 / 宣布失效", "abolished"),
    (("修订", "修改", "修正", "重新印发"), "条款修订 / 重新印发", "revised"),
    (("解读", "答记者问", "一图读懂", "政策问答"), "官方政策解读", "current"),
    (("消费提示", "选购", "质量提示", "提示", "提醒"), "官方消费提示", "current"),
    (("通告", "禁止", "上道路行驶", "查处"), "通行管控通告", "current"),
    # 只认「新国标」这类明确表述，避免把 2019 年的监督意见误标成新标准实施
    (("新国标", "强制性国家标准", "GB 17761", "GB17761"), "新国标实施", "current"),
    (("施行", "实施", "生效"), "正式施行", "historical"),
    (("印发", "发布", "公布", "出台", "下发"), "政策印发", "historical"),
    (("试行", "暂行", "试点"), "试行启动", "historical"),
    (("整治", "专项行动", "全链条", "专项治理"), "专项整治部署", "current"),
    (("登记", "上牌", "号牌"), "登记上牌规则落地", "historical"),
)

_DATE_TIME_RE = re.compile(r"((?:19|20)\d{2})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")
_DATE_DASH_RE = re.compile(r"((?:19|20)\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?")


def _event_of(text: str) -> Tuple[str, str]:
    """从标题/摘要推断事件描述与节点类型。"""
    for words, label, status in _EVENT_RULES:
        if any(word in text for word in words):
            return label, status
    return "政策动态", "historical"


_MONTH_DAY_RE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def _norm_date(y: int, m: int, d: Optional[int]) -> str:
    if not (1990 <= y <= date.today().year + 1) or not (1 <= m <= 12):
        return ""
    if d:
        try:
            return date(y, m, int(d)).isoformat()
        except ValueError:
            return ""
    return f"{y}-{m:02d}"


def _find_dates_with_context(
    text: str, default_year: Optional[int] = None
) -> List[Tuple[str, str]]:
    """
    抽取日期并带回上下文，供事件类型判定。

    支持三种写法：
        · 2020年12月7日 / 2020-12-07（完整日期）
        · 2020年12月（月份级）
        · 12月22日（缺年份，用发布年份补齐——政务新闻里极常见）

    返回 [(ISO 日期, 前后 30 字上下文), ...]，按出现顺序去重。
    """
    text = text or ""
    pairs: List[Tuple[str, str]] = []

    for match in _DATE_TIME_RE.finditer(text):
        value = _norm_date(int(match.group(1)), int(match.group(2)), match.group(3))
        if value:
            # 后窗口只取 14 字：紧跟在日期后的动词才是这个日期真正对应的事件。
            # 取太宽会把「12月22日印发……23日起实施」里下一句的"实施"也吸进来。
            pairs.append((value, text[max(0, match.start() - 24) : match.end() + 14]))

    for match in _DATE_DASH_RE.finditer(text):
        value = _norm_date(int(match.group(1)), int(match.group(2)), match.group(3))
        if value:
            pairs.append((value, text[max(0, match.start() - 24) : match.end() + 14]))

    if default_year:
        for match in _MONTH_DAY_RE.finditer(text):
            # 已经有「2020年12月7日」的，不再用缺年份规则重复添加
            if "年" in text[max(0, match.start() - 6) : match.start()]:
                continue
            value = _norm_date(default_year, int(match.group(1)), match.group(2))
            if value:
                pairs.append((value, text[max(0, match.start() - 24) : match.end() + 14]))

    seen: set = set()
    unique: List[Tuple[str, str]] = []
    for value, context in pairs:
        if value in seen:
            continue
        seen.add(value)
        unique.append((value, context))
    return unique


def _parse_publish_time(value: str) -> str:
    """把 2020/12/21 02:51:09、2020-12-21 统一成 YYYY-MM-DD。"""
    match = re.match(r"((?:19|20)\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", (value or "").strip())
    if not match:
        return ""
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return ""


def _title_windows(title: str) -> List[str]:
    """标题 8 字滑窗，用于判断一条命中是否指向这份文件。"""
    title = (title or "").strip()
    if len(title) < 8:
        return []
    return [title[i : i + 8] for i in range(0, max(1, len(title) - 7), 2)]


def _is_anchored(meta: PolicyMeta, blob: str) -> bool:
    """命中文本里是否出现了目标文件的文号或标题片段。"""
    if meta.doc_number and meta.doc_number in blob:
        return True
    return any(window in blob for window in _title_windows(meta.title))


def _theme_match(meta: PolicyMeta, text: str) -> bool:
    """
    判断一条结果是否还在"这个主题"上。

    口语词检索时，第 1 路偶尔会混进同地域但不同业务的文章
    （查"生孩子补贴"却召回《医保办法》修订）。用法定术语的前两字做词根匹配，
    可以把这类"同城不同事"的结果挡在时间轴之外。
    """
    blob = text or ""
    terms = list(getattr(meta, "search_terms", []) or [])
    if getattr(meta, "topic", ""):
        terms.append(meta.topic)
    for term in terms:
        if not term:
            continue
        if term in blob:
            return True
        stem = term[:2]
        if len(stem) >= 2 and stem in blob:
            return True
    return False


def _timeline_from_hits(
    channels: Sequence[ChannelSearchResult], meta: Optional[PolicyMeta] = None
) -> List[Dict[str, str]]:
    """
    从三路命中的官方结果中动态构建演进节点。

    真实检索里，每条结果都带发布日期，标题/摘要里也常有「2020年12月发布」
    「2024年5月开展全链条整治」这类表述。这些就是最真实的时间轴素材，
    比单纯展示「未能解析出时间节点」有价值得多。

    一条重要约束：如果某条结果的主题是「废止 / 宣布失效」，但它既没有出现
    目标文件的文号、也没有出现标题片段，就不能把这条写进本政策的时间轴——
    否则页面上会出现"别人的废止公告"，比没有时间轴更误导。
    """
    nodes: List[Dict[str, str]] = []
    seen_dates: set = set()

    for channel in channels:
        for hit in channel.hits:
            title = (hit.title or "").strip()
            snippet = (hit.snippet or "").strip()
            if not title:
                continue

            blob = f"{title} {snippet}"
            if any(word in title for word in ("废止", "失效")) and meta is not None:
                if not _is_anchored(meta, blob):
                    continue
            # 口语词场景：主题不一致的"同城不同事"结果不进入本政策时间轴
            if meta is not None and getattr(meta, "is_short_query", False):
                if not _theme_match(meta, title):
                    continue

            published = _parse_publish_time(hit.publish_time or "")
            default_year = int(published[:4]) if published else None

            # 标题里的日期最接近"事件本身"，其次是正文，最后才是网页发布时间
            candidates: List[Tuple[str, str]] = []
            candidates.extend(_find_dates_with_context(title, default_year))
            candidates.extend(_find_dates_with_context(snippet[:320], default_year))
            if published:
                candidates.append((published, title))

            # 同一条结果最多贡献 2 个节点：优先保留上下文里带
            # 印发/施行/废止等事件词的日期，避免把正文里的普通日期都搬上时间轴
            is_main_hit = bool(
                meta is not None
                and meta.title
                and normalize_title(meta.title)[:12] in normalize_title(title)
            )
            pub_year = int(published[:4]) if published else 0

            scored: List[Tuple[int, str, str]] = []
            for value, context in candidates:
                # 正文里引用的陈年日期（与页面发布年相差 1 年以上）多半是背景资料，
                # 不是本文档自身的事件节点；主政策页除外。
                if pub_year and not is_main_hit:
                    try:
                        if abs(int(value[:4]) - pub_year) > 1:
                            continue
                    except ValueError:
                        continue
                event_label, _status = _event_of(context)
                if event_label == "政策动态":
                    event_label, _status = _event_of(title[:40])
                weight = 0 if event_label == "政策动态" else 1
                scored.append((weight, value, context))
            scored.sort(key=lambda item: -item[0])

            taken = 0
            for _weight, value, context in scored:
                if value in seen_dates:
                    continue
                event, status = _event_of(context)
                if event == "政策动态":
                    # 日期附近没有事件词时，退回标题判断（标题里的"解读/通告"等）
                    event, status = _event_of(title[:40])
                seen_dates.add(value)
                nodes.append(
                    {
                        "date": value,
                        "event": event,
                        "doc": title[:70],
                        "status": status,
                    }
                )
                taken += 1
                if taken >= 2:
                    break

    nodes.sort(key=lambda node: node["date"])
    return nodes


def _event_family(label: str) -> str:
    """把不同措辞归到同一事件族，用于时间轴去重（政策印发 / 政策发布施行视为同类）。"""
    if any(word in label for word in ("印发", "发布", "年度", "出台")):
        return "publish"
    if "废止" in label:
        return "abolish"
    if "修订" in label:
        return "revise"
    if "新国标" in label:
        return "standard"
    if "解读" in label:
        return "interpret"
    if "提示" in label:
        return "notice"
    if "通告" in label:
        return "traffic"
    if "整治" in label:
        return "campaign"
    if "施行" in label:
        return "effective"
    return label


def _days_between(a: str, b: str) -> int:
    """两个 YYYY-MM 或 YYYY-MM-DD 字符串之间的天数差（无法解析时返回 0）。"""
    def _parse(value: str) -> Optional[date]:
        parts = (value or "").split("-")
        try:
            if len(parts) == 2:
                return date(int(parts[0]), int(parts[1]), 1)
            if len(parts) == 3:
                return date(int(parts[0]), int(parts[1]), int(parts[2]))
        except (TypeError, ValueError):
            return None
        return None

    left, right = _parse(a), _parse(b)
    if left is None or right is None:
        return 0
    return abs((right - left).days)


def build_timeline(
    meta: PolicyMeta, status_code: str, channels: Sequence[ChannelSearchResult]
) -> List[Dict[str, str]]:
    """基于要素与真实命中时间，拼装政策演进时间轴。"""
    timeline: List[Dict[str, str]] = []
    used_dates: set = set()

    if meta.publish_date:
        timeline.append(
            {
                "date": meta.publish_date,
                "event": "政策发布",
                "doc": f"《{meta.title}》" + (f"（{meta.doc_number}）" if meta.doc_number else ""),
                "status": "historical",
            }
        )
        used_dates.add(meta.publish_date)
    elif meta.doc_year:
        # 只有文号年份时，如实标注为"年份级"节点，不伪造月日
        timeline.append(
            {
                "date": str(meta.doc_year),
                "event": "文件所属年度（未获取精确印发日）",
                "doc": f"《{meta.title}》" + (f"（{meta.doc_number}）" if meta.doc_number else ""),
                "status": "historical",
            }
        )
        used_dates.add(str(meta.doc_year))

    # 从三路命中动态提取演进节点（含新政、整治、新国标等）
    for node in _timeline_from_hits(channels, meta):
        if node["date"] in used_dates:
            continue
        used_dates.add(node["date"])
        timeline.append(node)

    if meta.stated_expiry:
        expiry_passed = _expiry_passed(meta.stated_expiry)
        if meta.stated_expiry not in used_dates:
            timeline.append(
                {
                    "date": meta.stated_expiry,
                    "event": "有效期届满" if expiry_passed else "文件标注的有效期",
                    "doc": "原文附则规定的失效日",
                    "status": "expired" if expiry_passed else "current",
                }
            )

    # 同一事件族默认只保留一条，避免出现 3 个「新国标实施」；
    # 但如果同族节点跨度超过一年（例如 2023 旧版育儿补助 → 2025 新版生育补助），
    # 最早与最新都保留，让时间轴真正呈现"旧版 → 新版"的演进。
    main_title = normalize_title(meta.title)[:12] if meta.title else ""

    def _is_main(node: Dict[str, str]) -> bool:
        return bool(main_title) and main_title in normalize_title(node.get("doc", ""))

    groups: Dict[str, List[Dict[str, str]]] = {}
    for node in timeline:
        groups.setdefault(_event_family(node["event"]), []).append(node)

    merged_nodes: List[Dict[str, str]] = []
    for items in groups.values():
        items.sort(key=lambda item: item["date"])
        main_nodes = [node for node in items if _is_main(node)]
        keep = main_nodes[0] if main_nodes else items[-1]
        first = items[0]
        if first["date"] != keep["date"] and _days_between(first["date"], keep["date"]) > 365:
            merged_nodes.append(first)
        merged_nodes.append(keep)

    # 节点过多时保留最早 1 个 + 最近 4 个：评审关心的是"从哪来、现在到哪"
    deduped: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for node in merged_nodes:
        deduped[(node["date"], node["event"], node["doc"])] = node
    timeline = sorted(deduped.values(), key=lambda node: node["date"])
    if len(timeline) > 5:
        # 截断时保住三个关键位置：主政策节点、最早的非主节点、最近 3 条。
        # 只取 [:1]+[-4:] 会把主政策挤掉或丢掉中间的关键转折（真实踩过的坑）。
        main_node = next((node for node in timeline if _is_main(node)), None)
        rest = [node for node in timeline if node is not main_node]
        tail = rest[-3:]
        first = rest[0] if rest and rest[0] not in tail else None
        picked = ([main_node] if main_node is not None else []) + ([first] if first else []) + tail
        unique = {(n["date"], n["event"], n["doc"]): n for n in picked}
        timeline = sorted(unique.values(), key=lambda node: node["date"])

    if not timeline:
        timeline.append(
            {
                "date": date.today().isoformat(),
                "event": "未从检索结果中提取到可核验日期",
                "doc": "建议补充发文字号或印发日期后重试",
                "status": "uncertain",
            }
        )

    return timeline


def _clip_sentences(text: str, limit: int = 220) -> str:
    """
    按整句截断，绝不出现半句话。

    顺序：先在 limit 内找最后一个「。」→「；」→「，」；
    都没有就【向后】找最近的句号/分号（允许超出 limit，最多到 3 倍）；
    整段都没有句读时返回空字符串——调用方据此跳过该维度，
    宁可不展示，也不拼出半句话。
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    for mark in ("。", "；", "，", ";", ","):
        cut = text.rfind(mark, 0, limit)
        if cut >= 30:
            return text[: cut + 1]

    # 范围内无句读：向后补齐到下一个完整句
    for mark in ("。", "；"):
        nxt = text.find(mark, limit)
        if 0 < nxt < limit * 3:
            return text[: nxt + 1]

    return ""


# 对比维度：按政策类型切换，避免用"补贴标准"去套《电动自行车管理办法》
_DIMENSION_SETS: Dict[str, Tuple[Tuple[str, Tuple[str, ...], str], ...]] = {
    "subsidy": (
        ("补贴标准 / 资助额度", ("补贴", "补助", "资助", "奖励", "万元", "额度", "标准"), "高风险（直接影响资金测算）"),
        ("申报主体与资格条件", ("申报", "条件", "资格", "主体", "范围", "认定"), "高风险（决定是否具备申报资格）"),
        ("有效期与申领时限", ("有效期", "截止", "时限", "逾期", "受理", "申报期"), "高风险（错过即无法补救）"),
    ),
    "regulation": (
        ("管理范围与核心定义", ("管理", "范围", "适用", "定义", "所称", "区域内"), "高风险（决定是否受本办法约束）"),
        ("准入标准与过渡期要求", ("标准", "国标", "准入", "过渡期", "认证", "CCC", "强制性"), "高风险（旧标车辆可能无法登记）"),
        ("登记上牌与通行监管要求", ("登记", "上牌", "号牌", "通行", "头盔", "载人", "处罚"), "中风险（直接影响日常使用）"),
    ),
    "generic": (
        ("适用范围与对象", ("适用", "范围", "对象", "主体"), "中风险（影响适用范围判断）"),
        ("核心管理要求", ("应当", "不得", "要求", "管理", "规范"), "高风险（直接影响合规动作）"),
        ("施行与衔接安排", ("施行", "有效期", "过渡期", "废止", "衔接"), "中风险（新旧衔接节点）"),
    ),
}


def _split_sentences(text: str) -> List[str]:
    """按中文句读切句，保留标点；结果至少 12 字才视为有效句子。"""
    parts = re.split(r"(?<=[。；！？])", (text or "").strip())
    return [part.strip() for part in parts if len(part.strip()) >= 12]


def _pick_sentence(
    channels: Sequence[ChannelSearchResult],
    keywords: Sequence[str],
    used: set,
) -> str:
    """
    在指定通道里挑一句与维度最相关、且语义完整的句子（返回原句，由调用方截断）。

    打分 = 命中关键词个数；同分取更长的句子（信息量更大）。
    used 里存的是【原句】，这样同一条款不会被两个维度重复引用。
    """
    best = ""
    best_score = 0
    for channel in channels:
        for hit in channel.hits:
            for sentence in _split_sentences(hit.snippet):
                if sentence in used:
                    continue
                score = sum(1 for word in keywords if word in sentence)
                if score <= 0:
                    continue
                if score > best_score or (score == best_score and len(sentence) > len(best)):
                    best, best_score = sentence, score
    return best


def build_diffs_from_evidence(
    meta: PolicyMeta, channels: Sequence[ChannelSearchResult]
) -> List[Dict[str, str]]:
    """
    从证据中提炼条款差异。

    两条硬规则（都是为了让截图经得起追问）：
        1. 旧规只从第 1/2 路（原文、废止目录）取，新规只从第 3 路（最新接续/解读）取，
           两个版本必须来自不同时期的不同文件；
        2. 两段文本相似度 ≥ 70% 时直接丢弃该维度——宁可少一组，
           也不把同一段话同时贴在旧规和新规里充当"对比"。
    """
    category = (getattr(meta, "policy_category", "") or "generic").lower()
    rules = _DIMENSION_SETS.get(category, _DIMENSION_SETS["generic"])

    # 旧规必须来自第 1 路（原文）；第 2 路是清理目录，里面没有实体条款，
    # 只在前者缺失时才作为兜底。
    old_channels = [ch for ch in channels if ch.channel_key == "origin"]
    if not old_channels:
        old_channels = [ch for ch in channels if ch.channel_key == "abolish"] or list(channels)
    new_channels = [ch for ch in channels if ch.channel_key == "replacement"]
    if not new_channels:
        new_channels = [ch for ch in channels if ch.channel_key != "origin"]

    diffs: List[Dict[str, str]] = []
    used_old: set = set()
    used_new: set = set()

    for dimension, keywords, impact in rules:
        old_raw = _pick_sentence(old_channels, keywords, used_old)
        new_raw = _pick_sentence(new_channels, keywords, used_new)
        if not old_raw or not new_raw:
            continue
        old_text = _clip_sentences(old_raw, 200) or old_raw
        new_text = _clip_sentences(new_raw, 200) or new_raw
        if old_text == new_text:
            continue
        # 相似度过高说明两句话其实出自同一版本的同一段表述，不能算"新旧对比"
        if difflib.SequenceMatcher(None, old_text, new_text).ratio() >= 0.7:
            continue

        used_old.add(old_raw)
        used_new.add(new_raw)
        diffs.append(
            {
                "dimension": dimension,
                "old_clause": old_text,
                "new_clause": new_text,
                "impact_level": impact,
            }
        )
        if len(diffs) >= 3:
            break

    if not diffs:
        anchor_raw = _pick_sentence(
            list(channels),
            ("适用", "管理", "规定", "办法", "通知", "补贴", "标准", "登记"),
            set(),
        )
        anchor = (_clip_sentences(anchor_raw, 200) or anchor_raw) if anchor_raw else ""
        diffs.append(
            {
                "dimension": rules[0][0],
                "old_clause": anchor or f"未检索到《{meta.title}》的条款原文。",
                "new_clause": "本次检索未取得同维度的现行条款表述，请展开信源核对最新通知原文，或补充发文字号后重试。",
                "impact_level": "待核验（证据不足）",
            }
        )

    return diffs


def build_sources(
    channels: Sequence[ChannelSearchResult],
    per_channel: int = 3,
    limit: int = 9,
) -> List[Dict[str, str]]:
    """
    三路均衡聚合信源。

    为什么不能简单地"全量合并再按可信度排序"
    ----------------------------------------
    真实检索里同一份国务院文件会被十几个区县站点转载，标题一字不差；
    全量合并后模块 06 会连续出现 8 条同名链接，非常像刷屏。

    这里的策略是：
        1. 每一路各取前 per_channel 条（原文 / 废止目录 / 最新接续各占一席）；
        2. 跨路按「归一化标题」去重，同名文件只保留最权威的一条；
        3. 保持 通道1 → 通道2 → 通道3 的顺序，让信源列表自带叙事：
           旧政原文 → 废止公告 → 现行替代。
    """
    picked: List[Dict[str, str]] = []
    seen_keys: List[str] = []

    for channel in channels:
        taken = 0
        for hit in sorted(channel.hits, key=lambda h: host_rank(h.site)):
            key = normalize_title(hit.title) or hit.url
            if any(titles_conflict(key, existing) for existing in seen_keys):
                continue
            seen_keys.append(key)
            picked.append(
                {
                    "title": hit.title,
                    "url": hit.url,
                    "source_type": hit.source_type or "检索结果",
                    "publish_time": hit.publish_time,
                    "trust_level": hit.trust_level or "待评级",
                }
            )
            taken += 1
            if taken >= per_channel:
                break

    return picked[:limit]


# ---------------------------------------------------------------------------
# Mock 案例直出（演示模式的"地面真值"）
# ---------------------------------------------------------------------------
def judge_from_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """
    从内置案例中读取已编排好的判定结果。

    这不是"偷懒"：演示模式下的案例本身就是人工核验过的标注数据，
    走这条路径才能保证截图里的时间轴、Diff、信源与结论完全自洽。
    """
    payload = copy.deepcopy(case)
    result = payload.get("validity_result", {})
    code = result.get("status_code", "UNCERTAIN")
    status = _status_meta(code)

    # 补齐展示字段（Mock 数据里可能没写 emoji）
    result.setdefault("status_label", status["status_label"])
    result.setdefault("status_color", status["status_color"])
    result["status_emoji"] = status.get("emoji", "")
    result.setdefault("confidence_score", 80)
    result.setdefault("authority_score", 90)

    return {
        "validity_result": result,
        "timeline": payload.get("timeline", []),
        "clause_diffs": enrich_clause_diffs(payload.get("clause_diffs", [])),
        "sources": payload.get("sources", []),
    }


# ---------------------------------------------------------------------------
# 大模型证据文本拼装
# ---------------------------------------------------------------------------
