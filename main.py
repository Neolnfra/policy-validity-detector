"""
main.py —— FastAPI 主入口
================================================================================

启动方式
--------
    # 1) 安装依赖
    pip install -r requirements.txt

    # 2) 直接运行（无需任何 Key，自动进入高仿真演示模式）
    python main.py

    # 3) 或使用 uvicorn（开发热重载）
    uvicorn main:app --reload --port 8000

访问
----
    大屏首页      http://127.0.0.1:8000/
    Swagger 文档  http://127.0.0.1:8000/docs
    ReDoc         http://127.0.0.1:8000/redoc
    健康检查      http://127.0.0.1:8000/api/v1/health
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from api.routes import router as api_router
from api.webhook import router as webhook_router
from config import MOCK_CASES_FILE, STATIC_DIR, settings
from core.yuanbao_client import get_mock_index

# Windows 控制台默认 GBK，输出中文横幅可能抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 —— 重定向到不支持 reconfigure 的流时跳过
        continue


BANNER = r"""
╔══════════════════════════════════════════════════════════════════════════╗
║   政策过期检测器  Policy Validity Detector                                ║
║   元宝搜索驱动 · 零幻觉政务核验 Agent                                     ║
╠══════════════════════════════════════════════════════════════════════════╣
║   三路求证：原文溯源 → 废止排查 → 新政替代                                ║
╚══════════════════════════════════════════════════════════════════════════╝
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动自检：把运行模式、案例数、缺失项一次性打印清楚，减少"为什么没生效"的排查成本。"""
    print(BANNER)
    info = settings.describe()
    print(f"  运行模式     : {info['mode']}（{info['mode_label']}）")
    print(f"  搜索通道     : {info['provider_label']}")
    print("  判定引擎     : 确定性规则引擎（四态门槛 + 铁证锚定）")

    mock_index = get_mock_index()
    print(f"  内置案例     : {len(mock_index.cases)} 个")
    if not MOCK_CASES_FILE.exists():
        print(f"  [警告] 未找到数据集：{MOCK_CASES_FILE}，演示模式将退化为占位结果")
    if not STATIC_DIR.exists():
        print(f"  [警告] 未找到前端目录：{STATIC_DIR}，仅 API 可用")

    print(f"  首页         : http://{settings.host}:{settings.port}/")
    print(f"  API 文档     : http://{settings.host}:{settings.port}/docs")
    print("  按 Ctrl+C 停止服务\n")
    yield
    print("\n  服务已停止。")


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=settings.description,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "Policy Validity Detector", "url": "https://www.gov.cn/zhengce/"},
    license_info={"name": "MIT"},
)

# ---------------------------------------------------------------------------
# 中间件
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_header(request: Request, call_next):
    """给每个响应打上运行模式标记，便于第三方排查"我拿到的是真实数据还是演示数据"。"""
    response = await call_next(request)
    response.headers["X-Policy-Mode"] = settings.search_mode
    response.headers["X-Policy-Version"] = settings.version
    return response


@app.middleware("http")
async def disable_frontend_cache(request: Request, call_next):
    """
    前端资源强制协商缓存。

    背景：StaticFiles 默认只发 ETag / Last-Modified，不发 Cache-Control，
    浏览器会按启发式规则直接复用本地副本，导致改了 index.html / app.js
    之后用户仍看到旧界面（"明明改了却没生效"）。这里显式要求每次校验。
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# ---------------------------------------------------------------------------
# 全局异常兜底：任何未捕获异常都转成结构化 JSON，不把堆栈糊到前端
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "code": 500,
            "error": type(exc).__name__,
            "detail": str(exc)[:500],
            "path": str(request.url.path),
            "hint": "若是真实接口调用失败，请检查 .env 凭证；系统已内置 Mock 降级，通常不会影响页面演示。",
        },
    )


# ---------------------------------------------------------------------------
# 路由挂载
# ---------------------------------------------------------------------------
app.include_router(api_router)
app.include_router(webhook_router)


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    """挂载单页大屏。"""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse(
            status_code=404,
            content={
                "code": 404,
                "detail": "未找到 static/index.html",
                "hint": "请确认前端文件已随工程一同下载。API 本身仍可访问：/docs",
            },
        )
    return FileResponse(index_file, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """避免浏览器 404 噪音。"""
    return Response(status_code=204)


@app.get("/api/v1/meta", include_in_schema=False)
async def meta():
    """给前端一次性拿到运行模式 + 案例 + 行政区划的轻量入口（可选使用）。"""
    return {
        "code": 200,
        "settings": settings.describe(),
        "cases": get_mock_index().presets(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=bool(os.getenv("RELOAD", "")),
        log_level="info",
    )
