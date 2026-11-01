"""
core/runtime_config.py —— 运行时密钥热插拔（Hot-swappable Credentials）
================================================================================

为什么需要这一层
----------------
原工程的所有凭证都在 `.env` 里，改一次 Key 必须重启进程。本模块提供一条
与进程启动完全解耦的凭证注入链路，让前端「引擎与密钥设置」面板填入的 Key
在不重启后端的情况下立即生效。

四级优先级（严格按此顺序，任何一级命中即停止向下查找）
-----------------------------------------------------
    ① 本次请求 Header / Body 传入的用户自定义密钥
    ② 后端运行时内存中保存的密钥（POST /api/v1/settings/config 写入）
    ③ `.env` 环境变量配置（进程启动时快照）
    ④ 内置高精度兜底引擎（Mock，见 yuanbao_client._mock_result）

设计约束（很重要）
------------------
1. **不落盘**：运行时密钥只存在于当前进程内存，不写文件、不写日志，
   进程重启即失效；前端另存 localStorage，刷新页面后可重新同步。
2. **不明文外泄**：任何对外响应只返回掩码（`sk-****8f9a`），
   `.env` 里的原始 Key 永不出现在 API 返回体中。
3. **不改全局单例**：`config.settings` 是进程级快照，被大量模块按属性读取。
   这里用浅拷贝派生出一份「本次请求的有效配置」，请求结束即丢弃，
   避免 A 用户的自定义 Key 污染 B 用户（多租户共用后端时的经典事故）。
4. **失败不阻断**：凭证缺失或格式异常只会让链路落到下一级，
   绝不抛异常打断核验流程。
"""

from __future__ import annotations

import copy
import hashlib
import threading
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

from config import Settings
from config import settings as default_settings

# ---------------------------------------------------------------------------
# 请求头契约：前端与后端的唯一约定，字段名一旦确定不要随意改动
# ---------------------------------------------------------------------------
HEADER_YUANBAO_KEY = "x-yuanbao-api-key"
HEADER_YUANBAO_BASE = "x-yuanbao-base-url"

# 单个请求头允许的最大长度：防止有人把整段私钥文件塞进 Header
_MAX_HEADER_LEN = 4096

# 运行时存储的字段白名单（避免前端传入任意字段污染 Settings 对象）
_RUNTIME_FIELDS = (
    "yuanbao_api_key",
    "yuanbao_base_url",
    "concurrency",
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Overrides:
    """一次请求的凭证覆盖项。空字符串表示「本层未提供」，不是「清空」。"""

    yuanbao_api_key: str = ""
    yuanbao_base_url: str = ""
    concurrency: str = ""
    sources: Dict[str, str] = field(default_factory=dict)

    def any(self) -> bool:
        """是否存在任何有效覆盖（不含来源标记）。"""
        return any(
            [
                self.yuanbao_api_key,
                self.yuanbao_base_url,
            ]
        )

    def fingerprint(self) -> str:
        """
        配置指纹：用于搜索缓存分桶。

        同一句 query 用不同 Key（不同额度/不同租户）调用必须视为两次独立请求，
        否则会出现「换 Key 后仍命中上一把 Key 的缓存」这种极难排查的问题。
        """
        if not self.any():
            return "env"
        raw = "\x1f".join(
            [
                self.yuanbao_api_key,
                self.yuanbao_base_url,
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 掩码
# ---------------------------------------------------------------------------
def mask_secret(value: str) -> str:
    """
    密钥掩码：保留前 4 位与后 4 位，中间固定 4 个星号。

    `SecretId|SecretKey` 双段格式会分别掩码，避免把 SecretKey 整段漏出去。
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    if "|" in raw:
        return "|".join(mask_secret(part) for part in raw.split("|", 1))
    if len(raw) <= 8:
        return "****"
    return f"{raw[:4]}****{raw[-4:]}"


def _clip_header(value: Optional[str]) -> str:
    if not value:
        return ""
    return str(value).strip()[:_MAX_HEADER_LEN]


# ---------------------------------------------------------------------------
# 运行时可写配置（进程内存）
# ---------------------------------------------------------------------------
_RUNTIME_LOCK = threading.RLock()
_RUNTIME: Dict[str, Any] = {key: "" for key in _RUNTIME_FIELDS}
_RUNTIME["updated_at"] = ""


def get_runtime() -> Dict[str, Any]:
    """返回运行时配置的只读快照（副本，外部修改不影响内部状态）。"""
    with _RUNTIME_LOCK:
        return dict(_RUNTIME)


def set_runtime(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """
    写入运行时配置。

    语义：只有出现在 payload 里的字段才会被更新；显式传空字符串等于清除该字段。
    `llm_shared` 取布尔值，其余字段一律按去除首尾空白后的字符串处理。
    """
    with _RUNTIME_LOCK:
        for key in _RUNTIME_FIELDS:
            if key not in payload:
                continue
            value = payload.get(key)
            text = "" if value is None else str(value).strip()[:_MAX_HEADER_LEN]
            _RUNTIME[key] = text
        _RUNTIME["updated_at"] = datetime.now().isoformat(timespec="seconds")
        return dict(_RUNTIME)


def clear_runtime() -> Dict[str, Any]:
    """清除全部运行时配置，回落到 `.env`。"""
    with _RUNTIME_LOCK:
        for key in _RUNTIME_FIELDS:
            _RUNTIME[key] = ""
        _RUNTIME["updated_at"] = ""
        return dict(_RUNTIME)


# ---------------------------------------------------------------------------
# 密钥格式解析
# ---------------------------------------------------------------------------
def split_yuanbao_secret(value: str) -> Tuple[str, str, str]:
    """
    解析元宝搜索密钥，兼容两种形态：

    - `Bearer Token`             -> ("", "", token)          走通用 REST 网关
    - `SecretId|SecretKey`       -> (SecretId, SecretKey, "") 走腾讯云 TC3 签名

    腾讯云官方「元宝搜索（WSA）」是 TC3-HMAC-SHA256 签名接口，单把 API Key
    无法直接调用，必须成对提供 SecretId / SecretKey。这里用竖线分隔的写法
    避免再增加一个输入框，同时让「只填一把 Bearer」的多数场景保持零心智负担。
    """
    raw = (value or "").strip()
    if not raw:
        return "", "", ""
    if "|" in raw:
        left, right = raw.split("|", 1)
        left, right = left.strip(), right.strip()
        if left and right:
            return left, right, ""
    return "", "", raw


def tencent_endpoint_kind(value: str) -> Tuple[str, str]:
    """
    识别腾讯云地址属于哪种接入方式，返回 (kind, host)。

    kind:
        "tc3"    —— `*.tencentcloudapi.com`，走 TC3-HMAC-SHA256 签名
        "apikey" —— `api.wsa.cloud.tencent.com`，走 `Authorization: Bearer`
        ""       —— 不是腾讯云地址，按第三方网关处理

    为什么必须区分：两种方式的鉴权完全不兼容。同一个 Key 发到错误的域名上，
    只会得到 404 或 401，而用户看到的报错跟"Key 填错了"一模一样。
    """
    raw = (value or "").strip()
    if not raw:
        return "", ""
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return "", ""
    if host.endswith("tencentcloudapi.com"):
        return "tc3", host
    if host == "api.wsa.cloud.tencent.com" or host.endswith(".wsa.cloud.tencent.com"):
        return "apikey", "api.wsa.cloud.tencent.com"
    if host == "wsa.cloud.tencent.com":
        # 文档里出现过的裸域名，按服务 API KEY 方式处理（这是当前主推的接入方式）
        return "apikey", "api.wsa.cloud.tencent.com"
    return "", host


def tencent_endpoint_host(value: str) -> str:
    """兼容旧调用：只关心 host。"""
    kind, host = tencent_endpoint_kind(value)
    if kind == "tc3":
        return host
    if kind == "apikey":
        return "wsa.tencentcloudapi.com"
    return ""


def _normalize_search_target(base_url: str, default_path: str) -> Tuple[str, str]:
    """
    把用户填写的 Base URL 归一化为 (base, path)。

    用户可能填 `https://gw.example.com`、`.../v1`、`.../v1/search` 三种形态，
    直接拼接默认路径会得到 `/v1/v1/search` 这种死链，因此在入口处收敛。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return "", default_path or "/v1/search"

    parsed = urlparse(base if "://" in base else "https://" + base)
    path = (parsed.path or "").rstrip("/")
    # 用户直接粘了完整端点（例如 /v1/search、/search、/api/query）：
    # 再拼一次默认路径会得到 /v1/search/v1/search 这种死链，因此原样使用。
    if path:
        return base, ""

    for suffix in ("/v1/search", "/search"):
        if base.endswith(suffix):
            return base[: -len(suffix)], suffix
    if base.endswith("/v1"):
        return base, "/search"
    return base, default_path or "/v1/search"


# ---------------------------------------------------------------------------
# 合并：请求 > 运行时 > .env
# ---------------------------------------------------------------------------
def overrides_from_headers(headers: Mapping[str, str]) -> Overrides:
    """
    从请求头构造覆盖项。

    Starlette 的 Headers 本身大小写不敏感，但为了兼容普通 dict（单测/脚本调用），
    这里统一转成小写键再取值。
    """
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    return Overrides(
        yuanbao_api_key=_clip_header(lowered.get(HEADER_YUANBAO_KEY)),
        yuanbao_base_url=_clip_header(lowered.get(HEADER_YUANBAO_BASE)),
    )


def resolve_overrides(request_ov: Optional[Overrides] = None) -> Overrides:
    """
    把「请求级覆盖」与「运行时配置」合并成一份最终覆盖项，并记录每个字段的来源。

    来源标记会随 /settings/status 返回，供前端提示「当前生效的是浏览器里的 Key
    还是服务器 .env 里的 Key」——排查"为什么填了没生效"时这一步非常关键。
    """
    runtime = get_runtime()
    merged = Overrides()
    sources: Dict[str, str] = {}

    for name in ("yuanbao_api_key", "yuanbao_base_url"):
        from_request = getattr(request_ov, name, "") if request_ov else ""
        if from_request:
            setattr(merged, name, from_request)
            sources[name] = "request"
        elif runtime.get(name):
            setattr(merged, name, runtime[name])
            sources[name] = "runtime"
        else:
            sources[name] = "env"

    merged.concurrency = (request_ov.concurrency if request_ov else "") or runtime.get("concurrency", "") or "three_way"
    merged.sources = sources
    return merged


def apply_overrides(base: Settings, ov: Overrides) -> Settings:
    """
    基于全局配置派生一份「本次请求的有效配置」。

    使用浅拷贝而不是直接改 `default_settings`：Settings 的属性全是字符串/数字/列表，
    浅拷贝后逐个覆盖即可，既不影响其它并发请求，也不需要重新读一遍 .env。
    """
    if ov is None:
        return base

    eff: Settings = copy.copy(base)
    # 并发策略不进 Settings 的 .env 体系，但挂在同一份"有效配置"上，
    # 让编排层（run_verification）不必再判断它来自请求、运行时还是默认值。
    eff.concurrency = ov.concurrency or "three_way"  # type: ignore[attr-defined]

    if not ov.any():
        return eff

    # ---------------- 元宝搜索 ----------------
    secret_id, secret_key, bearer = split_yuanbao_secret(ov.yuanbao_api_key)
    if secret_id and secret_key:
        eff.tc_secret_id = secret_id
        eff.tc_secret_key = secret_key
        eff.yuanbao_provider = "tencent_cloud"
    elif bearer:
        eff.yuanbao_api_key = bearer
        # 单把 Key 默认走「服务 API KEY 兼容模式」：不用理解签名，也不用填域名
        eff.yuanbao_provider = "apikey"

    # Base URL：请求级 > 运行时 > .env
    # 先判断它是不是腾讯云官方端点——是的话走 TC3，URL 里的路径一律忽略。
    eff.search_config_hint = ""  # type: ignore[attr-defined]
    chosen_base = ov.yuanbao_base_url
    if chosen_base:
        kind, host = tencent_endpoint_kind(chosen_base)
        parsed_base = urlparse(chosen_base if "://" in chosen_base else "https://" + chosen_base)

        if kind == "tc3":
            eff.tc_endpoint = host
            # 判定"这次到底能不能签名"，要看最终生效的凭证，而不是只看用户刚填的那一格：
            # 留空时用的是 .env 里成对的 SecretId/SecretKey，同样是合法配置。
            using_tc3 = bool(secret_id and secret_key) or (
                not ov.yuanbao_api_key and bool(eff.tc_secret_id and eff.tc_secret_key)
            )
            if not using_tc3:
                # 单段令牌 + 签名端点：签不了名，必须给出可执行的提示
                eff.search_config_hint = "tencent_requires_pair"  # type: ignore[attr-defined]

        elif kind == "apikey":
            eff.apikey_base = f"https://{host}"
            eff.apikey_path = (parsed_base.path or "").rstrip("/") or base.apikey_path or "/SearchPro"
            if secret_id and secret_key and not bearer:
                # 成对密钥 + API KEY 域名：签名对不上，提示回到 TC3 域名
                eff.yuanbao_provider = "tencent_cloud"
                eff.search_config_hint = "apikey_url_with_pair"  # type: ignore[attr-defined]

        else:
            norm_base, norm_path = _normalize_search_target(chosen_base, base.yuanbao_search_path)
            eff.yuanbao_api_base = norm_base
            eff.yuanbao_search_path = norm_path
            if bearer:
                eff.yuanbao_provider = "generic"

    # 用户显式给了 Key 就说明他要真跑，此时必须解除 DEMO_MODE 的强制演示
    if eff.yuanbao_api_key or (eff.tc_secret_id and eff.tc_secret_key):
        eff.demo_mode = False

    return eff


def effective_settings(ov: Optional[Overrides] = None) -> Settings:
    """对外唯一入口：拿到本次请求应当使用的配置对象。"""
    return apply_overrides(default_settings, resolve_overrides(ov))


# ---------------------------------------------------------------------------
# 状态描述（给 /api/v1/settings/status 用）
# ---------------------------------------------------------------------------
def describe_status(ov: Optional[Overrides] = None) -> Dict[str, Any]:
    """
    汇总三层配置的可见状态。所有 Key 一律掩码，`.env` 的原文不出后端。
    """
    runtime = get_runtime()
    merged = resolve_overrides(ov)
    eff = apply_overrides(default_settings, merged)

    def pick(field_name: str) -> Tuple[str, str]:
        """返回 (生效值, 来源)。生效值优先取合并后的真实配置。"""
        source = merged.sources.get(field_name, "env")
        if field_name == "yuanbao_api_key":
            live = eff.tc_secret_id if eff.resolved_provider == "tencent_cloud" else eff.yuanbao_api_key
        elif field_name == "yuanbao_base_url":
            if eff.resolved_provider == "apikey":
                live = f"{(eff.apikey_base or '').rstrip('/')}{eff.apikey_path or '/SearchPro'}"
            elif eff.resolved_provider == "generic":
                live = eff.yuanbao_api_base
            else:
                live = eff.tc_endpoint
        else:
            live = ""
        return mask_secret(live or ""), source

    yb_key_masked, yb_key_source = pick("yuanbao_api_key")

    return {
        "server": {
            "provider": eff.resolved_provider,
            "provider_label": {
                "tencent_cloud": "腾讯云 · 元宝搜索（WSA · TC3 签名）",
                "apikey": "元宝搜索 · 服务 API KEY（Bearer 鉴权）",
                "generic": "通用 REST 搜索网关（Bearer）",
                "mcp": "MCP over HTTP",
                "none": "未配置（本地高仿真引擎）",
            }.get(eff.resolved_provider, eff.resolved_provider),
            "mode": eff.search_mode,
            "mode_label": "元宝搜索真实接口模式" if eff.is_real_api else "高仿真演示模式",
            "search_configured": eff.search_configured,
            "env_search_configured": default_settings.search_configured,
        },
        "runtime": {
            "active": bool(runtime.get("yuanbao_api_key")),
            "yuanbao_key": mask_secret(runtime.get("yuanbao_api_key", "")),
            "yuanbao_base_url": runtime.get("yuanbao_base_url", ""),
            "concurrency": runtime.get("concurrency", "") or "three_way",
            "updated_at": runtime.get("updated_at", ""),
        },
        "effective": {
            "yuanbao_key": yb_key_masked,
            "yuanbao_key_source": yb_key_source,
            "endpoint": f"{(eff.apikey_base or '').rstrip('/')}{eff.apikey_path or '/SearchPro'}"
            if eff.resolved_provider == "apikey"
            else (eff.tc_endpoint if eff.resolved_provider == "tencent_cloud" else eff.yuanbao_api_base),
        },
        "defaults": {
            # 默认地址 = 服务 API KEY 兼容模式端点；用户留空时后端也用它
            "yuanbao_base_url": (default_settings.apikey_base or "").rstrip("/"),
            "yuanbao_endpoint": (
                f"{default_settings.apikey_base.rstrip('/')}{default_settings.apikey_path or '/SearchPro'}"
            ),
            "auth_hint": "只填 API Key 即可，Base URL 留空则使用默认端点",
        },
        "headers": {
            "yuanbao_api_key": HEADER_YUANBAO_KEY,
            "yuanbao_base_url": HEADER_YUANBAO_BASE,
        },
    }


def humanize_http_error(exc: Exception) -> str:
    """
    把底层异常翻译成用户能照着排查的一句话。

    前端只有一个提示条，塞 `httpx.ConnectError(...)` 这种堆栈没有意义。
    """
    name = type(exc).__name__
    text = str(exc)

    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code == 401:
        return "鉴权失败（401）：API Key 无效、已过期或与当前接口不匹配。"
    if status_code == 403:
        return "无访问权限（403）：Key 有效但未开通该接口，或 IP 不在白名单。"
    if status_code == 404:
        return "接口不存在（404）：请检查 Base URL 与检索路径是否正确。"
    if status_code == 429:
        return "触发限流（429）：调用频率或额度已超限，请稍后重试。"
    if status_code and 500 <= status_code < 600:
        return f"服务端错误（{status_code}）：上游接口暂时不可用，请稍后重试。"

    # 元宝搜索的业务错误以 RuntimeError 形式抛出，先剥掉类型前缀再判断
    plain = text.split(":", 1)[1].strip() if text.startswith("RuntimeError:") else text

    # 先认签名类错误：它同样带 AuthFailure 前缀，顺序反过来会被误判成 API KEY 问题
    if "SecretIdNotFound" in plain or "Signature" in plain or "SignatureExpire" in plain:
        return "腾讯云签名校验失败：SecretId/SecretKey 不匹配或已轮换，请重新生成后再填。"
    if "UnauthorizedOperation" in plain or "AuthFailure" in plain:
        return (
            "服务 API KEY 鉴权未通过：请确认这把 Key 是在「元宝搜索控制台 → 服务 API KEY」里创建的，"
            "未过期、属于当前账号，且服务已开通。"
        )

    lowered = plain.lower()
    if "timeout" in lowered or "timed out" in name.lower():
        return "连接超时：网络不可达或接口响应过慢，请检查 Base URL 与出网策略。"
    if "name or service not known" in lowered or "getaddrinfo" in lowered or "nodename" in lowered:
        return "域名解析失败：Base URL 的域名不存在或 DNS 不可用。"
    if "certificate" in lowered or "ssl" in lowered:
        return "TLS 证书校验失败：请确认接口地址为 https 且证书有效。"
    if "connect" in lowered:
        return "无法建立连接：请确认接口地址、端口与网络连通性。"
    return f"{name}: {plain[:160]}"
