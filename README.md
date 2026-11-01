# 政策过期检测器 · Policy Validity Detector

> 元宝搜索开发者征文大赛参赛作品 ｜ 主题：**少点幻觉，多点搜索**
>
> 一个用「元宝搜索」三路交叉求证，专门揪出**已废止 / 已到期 / 被新政替代**政策的核验 Agent。

通用大模型回答政务、补贴、法规类问题时，受限于语料截止时间，最典型的错误不是"答不出"，
而是**把一份三年前已失效的文件，当成现行政策信誓旦旦地告诉你**。本工程用一个可运行的
全栈系统回答一个问题：**这份文件，今天还算数吗？**

---

## 一、核心设计：为什么必须是"三路"

| 通道 | 检索目标 | 解决什么 | 只做这一路的后果 |
| --- | --- | --- | --- |
| **A 原文溯源** | `site:{gov.cn} "文号" 有效期 附则` | 文件怎么写的、有效期到哪天 | 官网原文永远显示"有效"，它不会自己标注失效 |
| **B 废止排查** | `"文号" 废止 OR 宣布失效 OR 清理结果` | 有没有被提前废掉 | 漏掉《废止和宣布失效的决定》这类"死刑判决书" |
| **C 新政替代** | `{地区} {主题} 最新 解读 一图读懂` | 现在到底按什么办 | 只能证明"旧的不算数"，说不出"该怎么办" |

三路交叉后才能得出四态结论：

- 🟢 **VALID** 现行有效
- 🟡 **REPLACED** 部分失效 / 已被新政替代
- 🔴 **EXPIRED** 已废止 / 到期失效
- ⚪ **UNCERTAIN** 存疑待人工核验（**这是一个合法结论**，系统宁可说"不确定"，也不编造）

### 口语词输入的处理链路

真实用户不会说"《湖南省电动自行车管理办法》"，只会输入"电瓶车"。这条链路的处理顺序是：

1. **术语映射**：`电瓶车 / 电动车` → `(电动自行车 OR 电动两轮车)`，用政务网真正使用的法定术语去检索；
2. **焦点锁定**：从第 1 路命中结果中按"文种 + 属地 + 权威度"打分，锁定《湖南省电动自行车管理办法》，**绝不把「电瓶车」拼成《电瓶车》**；
3. **属地过滤**：查湖南时，海南的登记规定、广东的转载、论坛问答一律剔除（域名白名单 + 外省地名黑名单双重校验）；
4. **动态时间轴**：从命中结果的标题日期与发布日期中提取 3~5 个真实演进节点，未与目标文件锚定的「废止公告」不得进入时间轴；
5. **条款对比**：旧规只取第 1 路原文、新规只取第 3 路接续政策，相似度 ≥70% 直接丢弃；维度名按政策类型切换（资金补贴类 vs 行政管理类）。

---

## 二、工程目录

```text
policy-validity-detector/
├── .env.example                 # 环境变量模板（三种搜索接入方式 + 降级开关）
├── requirements.txt             # 依赖清单（5 个包，无重型框架）
├── README.md                    # 本文档
├── config.py                    # 全局配置 + 真实/Mock 模式自动判定
├── main.py                      # FastAPI 入口，挂载 API 与静态前端
│
├── core/                        # 核心业务层（不依赖 FastAPI，可独立复用）
│   ├── __init__.py
│   ├── region_router.py         # 34 省级行政区 + 58 重点城市 + 36 部委 → 政务网域名路由
│   ├── query_builder.py         # 政策要素解析（文号/日期/机关）+ 三路检索词裂变
│   ├── yuanbao_client.py        # 元宝搜索适配器：TC3 签名 / REST / MCP + Mock 自动降级
│   └── validity_judge.py        # 四态判定、权威度评分、短语级条款 Diff
│
├── api/                         # 接口层
│   ├── __init__.py
│   ├── schemas.py               # Pydantic v2 请求/响应契约（/docs 自动生成）
│   ├── routes.py                # 核心 RESTful API + Agent 编排 + SSE 流式
│   └── webhook.py               # 企业微信 / 飞书机器人回调
│
├── mock/
│   └── national_cases.json      # 5 个高仿真案例（覆盖全部 4 种状态，含 30 条检索命中）
│
└── static/                      # 零构建前端
    ├── index.html               # 浅色现代 SaaS 风大屏（Vue 3 + Tailwind + 矢量印章）
    └── app.js                   # 仅负责调接口、渲染与动画，不含任何业务数据
```

---

## 三、四层架构

```text
[用户输入：政策名称 / 发文字号 / 正文片段 + 选填地区]
        │
        ▼
① 意图与要素解析层  core/query_builder.py + region_router.py
   · 识别行政层级（部委 / 省 / 市）→ 动态路由到 *.gov.cn
   · 提取《政策名》、发文字号、印发日期、原文标注有效期
   · 识别"试行/暂行"（这类文件生命周期通常只有 2 年）
        │
        ▼
② 元宝搜索多路求证层  core/yuanbao_client.py
   · 通道 A 原文溯源 → 通道 B 废止排查 → 通道 C 新政替代
   · 真实接口（腾讯云 TC3 / 通用 REST / MCP）失败 → 无缝降级本地引擎
        │
        ▼
③ 判定与 Diff 层  core/validity_judge.py
   · 四态状态机 + 权威度评分（S/A/B/D 级信源加权）
   · 字符级条款 Diff（difflib SequenceMatcher → 红绿分段）
   · 全程确定性规则：同一份证据永远得到同一个结论，可复现、可解释
        │
        ▼
④ 可视化与开放接口层  static/ + api/routes.py
   · App Shell 工作台：左导航四视图切换（判定总览 / 三路思考链 / 条款对比 / 信源溯源）+ 全景展开
   · SSE 流式思考链 · RESTful API · Swagger 文档 · Markdown 报告导出
```

---

## 四、快速开始（零配置，30 秒）

```bash
cd policy-validity-detector

# 1) 安装依赖
pip install -r requirements.txt

# 2) 启动（不配任何 Key，自动进入高仿真演示模式）
python main.py

# 3) 打开浏览器
#    大屏首页      http://127.0.0.1:8000/
#    Swagger 文档  http://127.0.0.1:8000/docs
```

启动后页面会自动跑一遍「北京案例（已失效）」，可直接截图。

> 想换端口：`PORT=8080 python main.py`，或 `uvicorn main:app --port 8080`

---

## 五、接入真实元宝搜索（三种形态，改 `.env` 即可）

```bash
cp .env.example .env
```

### 形态 A：腾讯云官方 API（元宝搜索 / 联网搜索 WSA）

```ini
YUANBAO_PROVIDER=tencent_cloud
YUANBAO_SECRET_ID=你的SecretId
YUANBAO_SECRET_KEY=你的SecretKey
YUANBAO_TC_ENDPOINT=wsa.tencentcloudapi.com
YUANBAO_TC_ACTION=SearchPro
YUANBAO_TC_VERSION=2025-05-08   # 以官方文档当前版本为准
```

工程内置了完整的 `TC3-HMAC-SHA256` 签名实现（`core/yuanbao_client.py::build_tc3_headers`），
**无需安装腾讯云 SDK**。Action / Version 请按你开通时的接口文档填写。

### 形态 B：通用 REST 搜索网关

```ini
YUANBAO_PROVIDER=generic
YUANBAO_API_KEY=sk-xxx
YUANBAO_API_BASE=https://your-gateway.com
YUANBAO_SEARCH_PATH=/v1/search
YUANBAO_QUERY_FIELD=query
```

返回体解析是"宽容"的：会自动在任意嵌套层级里寻找含 `url/link` 的文档列表，
兼容 `title/Title/name`、`content/snippet/summary` 等多种字段命名。

### 形态 C：MCP over HTTP

```ini
YUANBAO_PROVIDER=mcp
YUANBAO_MCP_URL=https://your-mcp-server/mcp
YUANBAO_MCP_TOKEN=xxx
YUANBAO_MCP_TOOL=yuanbao_search
```

走 JSON-RPC 2.0 `tools/call`，并兼容 Streamable HTTP 的 SSE 响应帧。

### 降级契约

任何真实调用出现 **未配置 / 超时 / 非 2xx / 空结果 / 解析失败**，都会静默降级到本地引擎，
并在返回体的 `search_trace[].degraded` 与 `error` 中标注原因。**前端永远不会因此报错白屏。**

---

## 六、接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 运行模式、provider、案例数、版本 |
| GET | `/api/v1/regions` | 34 省级行政区 + 58 城市 + 36 部委 + 5 个演示预设 |
| POST | `/api/v1/policy/parse` | 仅解析要素 + 生成三路检索词（不检索） |
| POST | `/api/v1/policy/verify` | **核心**：解析 → 三路检索 → 求证 → 判定（带 10 分钟缓存） |
| POST | `/api/v1/policy/verify/stream` | 同上的 SSE 版本，逐步推送思考链 |
| POST | `/api/v1/policy/diff` | 独立的新旧条款字符级 Diff |
| POST | `/api/v1/export/report` | 生成 Markdown 核验报告 |
| GET | `/api/v1/openapi/snippets` | 生成 cURL / Python / JS 调用示例 |
| POST | `/api/v1/webhook/wecom` | 企业微信群机器人回调 |
| POST | `/api/v1/webhook/feishu` | 飞书自定义机器人回调 |

### 调用示例

```bash
curl -X POST 'http://127.0.0.1:8000/api/v1/policy/verify' \
  -H 'Content-Type: application/json' \
  -d '{"raw_input":"《北京市关于加快新能源汽车推广应用的实施意见》（京政办发〔2020〕12号）","region_hint":"auto"}'
```

```python
import httpx

resp = httpx.post("http://127.0.0.1:8000/api/v1/policy/verify", json={
    "raw_input": "杭州市应届毕业生生活补贴（春雨计划）本科1万元、硕士3万元还有效吗？",
    "region_hint": "hangzhou",
}, timeout=60)

data = resp.json()
print(data["mode"])                                  # real_api | mock_fallback
print(data["validity_result"]["status_code"])        # VALID / REPLACED / EXPIRED / UNCERTAIN
print(data["validity_result"]["confidence_score"])   # 0-100
print(len(data["search_trace"]))                     # 3
print(data["clause_diffs"][0]["old_segments"])       # 红绿 Diff 分段
```

### SSE 流式事件序列

```text
route → parse → queries → search_start → search_done(×3) → judge → complete
```

前端用 `fetch` + `ReadableStream` 消费（`EventSource` 不支持 POST），
并在流式不可用时自动回退到普通 POST。

---

## 七、内置演示案例

| 预设 | 政策 | 状态 | 看点 |
| --- | --- | --- | --- |
| 🏛️ 全国部委 | 《推动大规模设备更新和消费品以旧换新行动方案》 | 🟡 REPLACED | 框架文件仍有效，但年度补贴细则已切换，范围扩围至国四 |
| 📍 北京 | 《北京市关于加快新能源汽车推广应用的实施意见》 | 🔴 EXPIRED | 附则有效期届满 + 列入失效目录，普惠购置补贴已取消 |
| 📍 杭州 | 《杭州市高层次人才分类认定办法》+ 春雨计划 | 🟢 VALID | 主文件有效，但参保月数、区县口径有微调 |
| 📍 深圳 | 《深圳市高新技术产业园区专项扶持资金管理办法（试行）》 | 🔴 EXPIRED | "试行"满 2 年未转正，2023 年被明令废止 |
| 📍 长沙 | 《长沙市智能制造首台（套）重大技术装备保险补偿申报细则》 | ⚪ UNCERTAIN | 园区级通知口径冲突，系统拒绝编造申报日期 |

每个案例包含：3 路检索式、6 条命中摘要、4 个时间轴节点、3 组条款 Diff、4 条权威信源。

> `mock/national_cases.json` 是**演示数据集**，政策名称与条款参照真实公文体系撰写，
> 但属演示样本，不作为办事依据。所有 `sources` 链接指向官方网站的**栏目入口页**
> （政策文件库 / 公报 / 解读），而非伪造的固定深链，以确保长期可访问。

---

## 八、验证与测试

### 1. 命令行验证核心链路

```bash
# 健康检查（看 mode 是否为 mock_fallback）
curl http://127.0.0.1:8000/api/v1/health

# 要素解析（看文号、属地、三路检索式是否正确）
curl -X POST http://127.0.0.1:8000/api/v1/policy/parse \
  -H 'Content-Type: application/json' \
  -d '{"raw_input":"长沙市智能制造首台（套）重大技术装备保险补偿申报细则（长工信发〔2023〕18号）"}'

# 观察 SSE 事件序列
curl -N -X POST http://127.0.0.1:8000/api/v1/policy/verify/stream \
  -H 'Content-Type: application/json' \
  -d '{"raw_input":"《深圳市高新技术产业园区专项扶持资金管理办法（试行）》（深府规〔2019〕8号）"}'
```

### 2. 配置自检

```bash
python config.py    # 打印当前模式、provider、路径是否存在
```

### 3. 关键断言（可直接用于征文截图说明）

| 断言 | 预期 |
| --- | --- |
| 未配置 Key 启动 | `mode = mock_fallback`，页面完整可用 |
| 错误 Key + 真实模式 | 三路检索 `degraded = true`，页面不报错 |
| 深圳案例（试行文件） | `status_code = EXPIRED`，命中废止决定 |
| 长沙案例（口径冲突） | `status_code = UNCERTAIN`，且**不输出具体申报日期** |
| 随机输入（无匹配案例） | `status_code = UNCERTAIN`，信源标注"演示占位（未联网）" |
| 导出的报告 | 含判定、要素、对比、思考链、时间轴、Diff、信源七节 |

---

## 九、几个刻意的技术取舍

**1. 宁可留空，也不伪造日期。**
只识别到文号年份时，`publish_date` 保持空字符串，时间轴显示"YYYY（文件所属年度）"。
把 `doc_year + 01-01` 伪装成精确印发日，本身就是一种幻觉。

**2. 没匹配到案例时，宁可返回 UNCERTAIN。**
演示模式下输入任意政策，若未命中内置案例，检索结果是明确标注的"演示占位"，
判定引擎据此输出"存疑待人工核验"，而不是套用相似案例的结论。

**3. 判定不依赖任何外部模型。**
四态结论由确定性规则按证据推导，同一份证据永远得到同一个结论；搜索断网时自动降级到本地引擎，
页面不会报错，结论会标注"证据链未闭合"。

**4. 前端零业务数据。**
`static/` 里没有任何政策文本、判定结论或检索式，全部来自 `/api/v1/*`。
换一套后端，前端不用改一行。

---

## 十、免责声明

本项目为技术演示与参赛作品，输出结论仅用于辅助判断政策时效性，**不替代主管部门的正式答复**。
涉及资金申报、资格认定的，务必以政务网原文与当期申报通知为准。

数据来源合法性说明：演示数据集不含任何未公开信息、客户数据或个人隐私；
真实模式下仅调用你自行配置的搜索接口，不落库、不外传。

---

**License**: MIT ｜ **Python**: 3.10+（已在 3.13 验证）｜ **前端**: 零构建，纯 CDN

## 演示与截图

本仓库只保留源代码与本文档；实测截图、演示视频与参赛文章随征文发布，不入库。

截图复现方式：`python main.py` 起真实检索实例（:8000），
`DEMO_MODE=true python -m uvicorn main:app --port 8001` 起演示实例，再用浏览器按 `1600×1000` 打开对应视图。

## 引擎与密钥设置（无需重启的热插拔）

侧栏左下角「引擎与密钥设置」可在浏览器里直接填写元宝搜索凭证，保存即生效，不必改 `.env`、不必重启进程。

**读取优先级（逐级回落）**

1. 单次请求的 Header：`X-Yuanbao-Api-Key`、`X-Yuanbao-Base-Url`、`X-Llm-Api-Key`、`X-Llm-Base-Url`、`X-Llm-Model`
2. 后端运行时内存（由 `POST /api/v1/settings/config` 写入）
3. `.env` 环境变量
4. 内置演示引擎

**相关接口**

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/v1/settings/status` | 三层配置的生效情况，Key 一律掩码 |
| POST | `/api/v1/settings/config` | 写入运行时配置，立即对后续请求生效 |
| DELETE | `/api/v1/settings/config` | 清除运行时配置，回落 `.env` |
| POST | `/api/v1/settings/test` | 用候选 Key 打一次真实请求，返回可读的失败原因 |

**凭证格式（按简单程度排序）**

1. **服务 API KEY（推荐）**：控制台 `元宝搜索 → 服务 API KEY → 创建`，把 `sk-` 开头的密钥填进面板即可。
   端点固定为 `POST https://api.wsa.cloud.tencent.com/SearchPro`，鉴权 `Authorization: Bearer <KEY>`，
   请求体与 TC3 完全一致。**Base URL 留空就用这个默认端点。**
2. **腾讯云标准方式（可选）**：`SecretId|SecretKey` 竖线分隔，走 `wsa.tencentcloudapi.com` 的 TC3 签名。
3. **第三方网关**：填网关的 Bearer Token + Base URL（支持根地址或完整端点）。

> 判定、时间轴、条款对比全部由确定性规则引擎完成，不依赖外部大模型；因此**只配置搜索这一项**即可跑通全链路。

**安全约束**：运行时 Key 只存在进程内存与浏览器 `localStorage`，不落盘、不写日志；接口响应只返回 `sk-****8f9a` 形式的掩码；`.env` 中的原始 Key 永不出现在 API 返回体里。
