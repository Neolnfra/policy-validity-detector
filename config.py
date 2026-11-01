"""
config.py —— 全局配置管理与【真实 API / Mock 模式】自动降级策略
================================================================================

设计要点
--------
1. 单一配置出口：全工程只允许通过 `from config import settings` 读取配置，
   禁止在业务代码里散落 os.getenv()。
2. 模式自动降级：`settings.search_mode` 会综合 DEMO_MODE 与已配置的凭证，
   自动得出 "real_api" 或 "mock_fallback"。业务层永远不需要自己判断，
   只要读 mode 做日志/展示即可；真正的降级发生在调用失败时（见 yuanbao_client）。
3. 配置不完整不算错误：缺少 Key 是「演示可跑」而非「启动失败」，这是本工程
   的核心诉求（评委零配置拉代码即可跑出截图）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
MOCK_DIR: Path = BASE_DIR / "mock"
STATIC_DIR: Path = BASE_DIR / "static"
MOCK_CASES_FILE: Path = MOCK_DIR / "national_cases.json"

# 优先读取工程根目录下的 .env；不存在时静默跳过（不影响启动）
load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)

SearchMode = Literal["real_api", "mock_fallback"]


# ---------------------------------------------------------------------------
# 小工具：类型安全的取值函数
# ---------------------------------------------------------------------------
def _str(key: str, default: str = "") -> str:
    value = os.getenv(key, default)
    if value is None:
        return default
    return value.strip()


def _int(key: str, default: int) -> int:
    try:
        return int(str(os.getenv(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(str(os.getenv(key, default)).strip())
    except (TypeError, ValueError):
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _list(key: str, default: List[str] | None = None) -> List[str]:
    raw = _str(key)
    if not raw:
        return default or []
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------
class Settings:
    """运行时配置快照。实例化一次，全局复用。"""

    def __init__(self) -> None:
        # ---------------- 应用元信息 ----------------
        self.app_name: str = "政策过期检测器 Policy Validity Detector"
        self.version: str = "1.0.0"
        self.description: str = (
            "基于元宝搜索三路交叉求证机制的全国政策时效性核验 Agent。"
            "核心能力：政务网原文溯源 → 废止清理目录排查 → 替代新政与官微解读比对。"
        )
        self.host: str = _str("HOST", "127.0.0.1")
        self.port: int = _int("PORT", 8000)

        # ---------------- 运行模式 ----------------
        self.demo_mode: bool = _bool("DEMO_MODE", False)

        # ---------------- 元宝搜索 ----------------
        self.yuanbao_provider: str = _str("YUANBAO_PROVIDER", "auto").lower()

        # 模式 A：腾讯云官方 API（TC3 签名）
        self.tc_secret_id: str = _str("YUANBAO_SECRET_ID")
        self.tc_secret_key: str = _str("YUANBAO_SECRET_KEY")
        self.tc_endpoint: str = _str("YUANBAO_TC_ENDPOINT", "wsa.tencentcloudapi.com")
        # 元宝搜索（WSA）为无地域接口：传 X-TC-Region 会报 UnsupportedRegion，保持为空
        self.tc_region: str = _str("YUANBAO_TC_REGION", "")
        self.tc_action: str = _str("YUANBAO_TC_ACTION", "SearchPro")
        self.tc_version: str = _str("YUANBAO_TC_VERSION", "2025-05-08")

        # 模式 A2：服务 API KEY 兼容模式（单把 Key + 固定端点，Bearer 鉴权）
        # 官方口径：POST https://api.wsa.cloud.tencent.com/SearchPro
        # 请求体与 TC3 完全一致（Action/Version/Query/Cnt），区别只在鉴权方式。
        self.apikey_base: str = _str("YUANBAO_APIKEY_BASE", "https://api.wsa.cloud.tencent.com")
        self.apikey_path: str = _str("YUANBAO_APIKEY_PATH", "/SearchPro")

        # 模式 B：通用 REST 网关
        self.yuanbao_api_key: str = _str("YUANBAO_API_KEY")
        self.yuanbao_api_base: str = _str("YUANBAO_API_BASE", "https://api.yuanbao.example.com")
        self.yuanbao_search_path: str = _str("YUANBAO_SEARCH_PATH", "/v1/search")
        self.yuanbao_query_field: str = _str("YUANBAO_QUERY_FIELD", "query")

        # 模式 C：MCP over HTTP
        self.mcp_url: str = _str("YUANBAO_MCP_URL")
        self.mcp_token: str = _str("YUANBAO_MCP_TOKEN")
        self.mcp_tool: str = _str("YUANBAO_MCP_TOOL", "yuanbao_search")

        # 网络参数
        self.http_timeout: float = _float("YUANBAO_TIMEOUT", 12.0)
        self.http_retries: int = max(0, _int("YUANBAO_RETRIES", 2))
        self.search_top_k: int = max(1, _int("YUANBAO_TOP_K", 10))
        # 是否使用系统代理。国内直连腾讯云更快；企业内网强制代理时可设为 true
        self.trust_env: bool = _bool("YUANBAO_TRUST_ENV", False)

        # ---------------- 其它 ----------------
        self.cors_origins: List[str] = _list("CORS_ORIGINS", ["*"])
        self.webhook_token: str = _str("WEBHOOK_TOKEN")

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------
    @property
    def resolved_provider(self) -> str:
        """把 auto 解析为具体 provider；未配置任何凭证时返回 'none'。"""
        if self.yuanbao_provider != "auto":
            return self.yuanbao_provider
        if self.tc_secret_id and self.tc_secret_key:
            return "tencent_cloud"
        if self.yuanbao_api_key:
            # 显式配了第三方网关地址才算 generic；否则默认打元宝搜索官方
            # 的 API KEY 端点——这是"只填一把 Key 就能用"的关键一步。
            base = self.yuanbao_api_base or ""
            if base and "example.com" not in base:
                return "generic"
            return "apikey"
        if self.mcp_url:
            return "mcp"
        return "none"

    @property
    def search_configured(self) -> bool:
        """搜索通道是否具备真实调用条件。"""
        provider = self.resolved_provider
        if provider == "tencent_cloud":
            return bool(self.tc_secret_id and self.tc_secret_key)
        if provider == "apikey":
            return bool(self.yuanbao_api_key and self.apikey_base)
        if provider == "generic":
            return bool(self.yuanbao_api_key and self.yuanbao_api_base)
        if provider == "mcp":
            return bool(self.mcp_url)
        return False

    @property
    def search_mode(self) -> SearchMode:
        """
        全局运行模式：
        - DEMO_MODE=true            -> mock_fallback（强制演示）
        - 已配置搜索凭证            -> real_api
        - 未配置搜索凭证            -> mock_fallback（零配置可跑）
        """
        if self.demo_mode or not self.search_configured:
            return "mock_fallback"
        return "real_api"

    @property
    def is_real_api(self) -> bool:
        return self.search_mode == "real_api"

    # ------------------------------------------------------------------
    # 对外描述（供 /api/v1/health 与前端状态灯使用）
    # ------------------------------------------------------------------
    def describe(self) -> Dict[str, Any]:
        provider = self.resolved_provider
        provider_label = {
            "tencent_cloud": "腾讯云 · 元宝搜索（WSA · TC3 签名）",
            "apikey": "元宝搜索 · 服务 API KEY（Bearer）",
            "generic": "通用 REST 搜索网关",
            "mcp": "MCP over HTTP",
            "none": "未配置（本地高仿真引擎）",
        }.get(provider, provider)

        return {
            "mode": self.search_mode,
            "mode_label": "元宝搜索真实接口模式" if self.is_real_api else "高仿真演示模式",
            "provider": provider,
            "provider_label": provider_label,
            "search_configured": self.search_configured,
            "demo_mode": self.demo_mode,
            "version": self.version,
            "timeout": self.http_timeout,
            "retries": self.http_retries,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()


if __name__ == "__main__":  # 便于本地自检：python config.py
    import json

    print(json.dumps(settings.describe(), ensure_ascii=False, indent=2))
    print(f"BASE_DIR   = {BASE_DIR}")
    print(f"MOCK_FILE  = {MOCK_CASES_FILE} (exists={MOCK_CASES_FILE.exists()})")
    print(f"STATIC_DIR = {STATIC_DIR} (exists={STATIC_DIR.exists()})")
