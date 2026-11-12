# 采购公告采集 API

传入**采购公告列表页 URL** 与**项目编号**，自动抓取该页指向的同站点原公告与更正公告，
从详情页提取发布时间、投标截止时间与取消采购信息，按发布时间合并后返回：
当前是否可投标、有效截止时间、项目状态以及全部来源链接。

## 业务规则

| 规则 | 实现位置 |
| --- | --- |
| 只采集列表页指向的**同站点**详情链接，外站链接、登录/导航链接自动忽略 | `app/parser.py::extract_detail_links` |
| 按**发布时间**从早到晚合并原公告与更正公告 | `app/merger.py::merge_notices` |
| 更正公告**未提及截止时间**时，沿用此前已生效的截止时间 | `app/merger.py` |
| 更正公告给出新截止时间（如延期）时，以新时间覆盖 | `app/merger.py` |
| 公告明确**取消采购** → 状态 `closed`、不可投标，不再返回有效截止时间（旧时间仅在公告明细中保留） | `app/merger.py` |
| **任一关联详情页**抓取/解析失败（404、超时、结构异常等）→ 状态 `pending` 待核实，`effective_deadline` 强制为 `null`，**绝不把旧时间当作最终期限** | `app/merger.py` + `app/collector.py` |
| **抓齐本项目详情后复核**：再读一次列表页与已抓详情页，本项目关联链接增删、发布时间/截止时间/取消状态变化、或复核读取失败 → 状态 `pending`，清空 `effective_deadline`，并在 `changes` / `sources(role=changed)` 中列出变化来源 | `app/collector.py::_recheck` + `app/merger.py` |
| 无关项目的目录/详情变化不算本项目变化；内容稳定时仍按发布时间顺序合并 | `app/collector.py` |
| 发布时间缺失、缺少原公告、全部公告均无截止时间等信息链不完整情况，同样标记 `pending` | `app/merger.py` |

> 复核动机：采购站可能在“本轮抓列表”与“抓详情”之间发布更正（延期、取消、新增更正公告）。
> 只按首次抓到的内容合并，会把已经失效的旧截止时间当作有效期限返回给投标专员。
> 因此采集器在抓齐本项目全部详情后，会再核对一次列表与本项目详情；任何无法确认“未变化”的情况
> 都按 `pending` 处理，宁可让专员人工核实，也不返回旧期限。

### 项目状态（`status`）

- `open`：可投标，`effective_deadline` 为合并后的有效截止时间；
- `closed`：采购已取消，`can_bid=false`，无有效截止时间；
- `pending`：待核实，存在关联页面失败、复核发现变化或信息不完整，`effective_deadline=null`；
- `not_found`：列表页（含复核）未发现与该项目编号相关的公告。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 入口、路由、浏览器演示页
│   ├── collector.py   # 采集编排：列表发现 → 并发抓取 → 复核变化 → 归属判定 → 合并
│   ├── parser.py      # HTML 解析：同站链接发现、三层字段提取、取消识别
│   ├── merger.py      # 业务合并规则（时间继承、延期覆盖、取消、待核实、变化复核）
│   └── models.py      # 请求/响应 Pydantic 模型（含 changes 变化告警）
├── sample-site/       # 本地示例公告站点（nginx 静态托管）
│   ├── list.html      # 公告列表页（含外站/登录干扰链接）
│   ├── detail/        # 3 个项目的原公告/更正/取消详情页
│   └── changed/       # “抓取过程中公告被更正”的稳定样例（新旧两个版本）
├── scripts/
│   └── change_server.py  # 仅供本地演示/测试：按请求顺序返回新旧版本，复现变化
├── nginx/default.conf # 示例站点 nginx 配置（缺失页面返回真实 404）
├── tests/             # 解析/合并单测 + 静态服务器端到端测试 + 变动复核测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后：

| 服务 | 地址 | 说明 |
| --- | --- | --- |
| 公告采集 API | http://localhost:8000 | Swagger 文档：http://localhost:8000/docs |
| 浏览器演示页 | http://localhost:8000/ | 填表即可发起采集 |
| 示例公告站点 | http://localhost:8080/list.html | nginx 托管的静态采购公告 |
| 变动样例站点 | http://localhost:8091/list.html | 按请求顺序返回新旧版本，复现抓取过程中公告被更正 |

停止：`docker compose down`。

### 复现“抓取过程中公告被更正”

`change-site` 服务每次请求依次返回旧版本、再返回新版本，因此一次 API 调用即可复现
“先抓到旧期限、复核时发现更正”的时序：

```bash
# 默认场景 link_added：复核时列表多出延期更正链接
docker compose up --build
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8091/list.html","project_code":"GSB-2026-004"}'

# 切换场景（重建容器后生效）
CHANGE_SCENARIO=field_changed docker compose up --build
```

预期响应 `status=pending`、`can_bid=false`、`effective_deadline=null`，
`changes` 与 `sources(role=changed)` 列出变化来源。

可用场景：`stable`（内容稳定，仍按原更正顺序合并）、`link_added`（新增本项目更正链接）、
`field_changed`（原公告截止时间被改）、`cancel_added`（更正公告转为取消）、
`recheck_failed`（复核时详情页 404）、`recovered`（首次失败、复核时已可读）、
`unrelated_changed`（只有无关项目变化，本项目不受影响）。

> 直接打开浏览器刷新 `http://localhost:8091/list.html` 会消耗版本序列；
> 先调用 API 再刷新即可看到“下一页更正”的演示效果。

## API 用法

### POST（推荐）

```bash
curl -X POST http://localhost:8000/api/v1/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url":"http://localhost:8080/list.html","project_code":"GSB-2026-001"}'
```

### GET（便于浏览器快速验证）

```bash
curl "http://localhost:8000/api/v1/collect?list_url=http://localhost:8080/list.html&project_code=GSB-2026-001"
```

响应关键字段：

```json
{
  "project_code": "GSB-2026-001",
  "status": "open",
  "can_bid": true,
  "effective_deadline": "2026-11-15T14:00:00",
  "cancelled": false,
  "notices": [ { "kind": "original", "published_at": "...", "deadline": "..." } ],
  "sources": [ { "role": "list", "url": "..." }, { "role": "correction", "url": "..." } ],
  "changes": [],
  "warnings": []
}
```

复核发现变化时：

```json
{
  "status": "pending",
  "can_bid": false,
  "effective_deadline": null,
  "changes": [
    {
      "kind": "link_added",
      "url": "http://localhost:8091/detail/g004-v2-corr.html",
      "message": "复核发现列表新增本项目关联链接：…（首次抓取时不存在，可能为刚发布的更正）"
    }
  ],
  "sources": [
    { "role": "changed", "url": "http://localhost:8091/detail/g004-v2-corr.html", "title": "…" }
  ],
  "warnings": ["复核发现列表新增本项目关联链接：…"]
}
```

`changes.kind` 取值：`link_added` / `link_removed`（列表本项目关联链接增删）、
`field_changed`（已抓详情的发布时间/截止时间/取消状态变化）、
`recheck_failed`（复核读取失败或无法确认归属）、
`recheck_recovered`（首次失败、复核时已可读，仍需人工确认）。

## 示例站点内置的三个演示项目

| 项目编号 | 场景 | 预期结果 |
| --- | --- | --- |
| `GSB-2026-001` | 原公告 → 澄清更正（**未提截止时间，沿用原时间**）→ 延期更正（**新时间覆盖**） | `open`，有效截止时间 `2026-11-15 14:00` |
| `GSB-2026-002` | 原公告 → 取消公告（预算收回） | `closed`，不可投标，无有效截止时间 |
| `GSB-2026-003` | 原公告 → 更正页**已下线（404）** | `pending` 待核实，`effective_deadline=null`，来源中列出失败链接 |

另外列表页还包含：外站链接、登录导航链接、其他项目公告（`GSB-2026-099`）、
无关项目的坏链接（`GSB-2025-888`），用于验证同站过滤、项目归属判定与失败隔离。

变动样例（`sample-site/changed/`，由 `change-site` 服务或 `scripts/change_server.py` 提供）
使用第四个演示项目 `GSB-2026-004`：原公告 + 澄清更正，再叠加“延期更正”发布时序变化。

## 详情页解析策略（三层回退）

1. **结构化标记**：`data-field="published-at|deadline|cancelled|..."` 元素或
   `<meta property="article:published_time">`；
2. **标签匹配**：`发布时间 / 投标截止时间 / 响应文件递交截止时间 / 取消原因` 等
   中文标签，兼容 `th/td`、`dt/dd`、`li/p` 等布局，支持
   `2026-11-10 09:30`、`2026年11月15日 14时00分` 等常见写法；
3. **正文正则兜底**：在含“截止/递交/提交/受理”等时限语义的块级元素中识别日期，
   并排除“发布时间”句、资质业绩说明中的历史日期（如“近三年（2023年1月1日起）”）。

取消采购识别支持 `data-field="cancelled"` 标记、取消原因标签以及
“本项目取消采购 / 终止本次招标”等显式句式。

> 接入真实采购平台时，若页面结构不同，优先在详情页模板中补 `data-field` 标记，
> 或在 `app/parser.py` 顶部的标签/关键词元组中登记该平台的字段叫法。

## 本地开发与测试（不使用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 终端 A：托管示例公告页
cd sample-site && python -m http.server 8080
# 终端 B：启动 API
uvicorn app.main:app --reload --port 8000

# 终端 C：启动变动样例站点（按场景复现“抓取过程中公告被更正”）
python scripts/change_server.py --port 8090 --scenario link_added

# 测试（自带静态服务器与变动场景站点，无需外部依赖、无需联网）
pytest -q
```

> 变动样例站点仅用 Python 标准库；`pytest` 会在随机端口上启动它，
> 因此测试与 Docker Compose 都不依赖外网演示站点。

## 设计说明

- 列表页抓取失败直接返回 `502`；详情页失败不影响整体响应，而是按业务规则沉淀为
  `pending` + 失败来源链接，便于投标专员人工核实。
- 关联归属判定结合详情页项目编号与列表行上下文（含编号列），
  即使详情页下线（只能看到列表行）也能正确判定失败是否与目标项目相关；
  无关项目的坏链接不会污染查询结果。
- 详情页并发抓取（上限 5），统一超时、自定义 UA、跟随重定向。
- 抓齐本项目详情后复核列表与详情：本项目链接增删、发布时间/截止时间/取消状态变化
  或复核读取失败 → `pending`、`effective_deadline=null`、变化来源写入 `changes` 与
  `sources(role=changed)`。复核时若读到了新版本内容，明细以新版本为准，但状态仍为待核实。
- 复核只比较本项目链接（列表行含目标编号，或详情页编号命中）与已抓本项目详情；
  无关项目的目录变化、坏链接、其他项目详情变化都不产生 `changes`，内容稳定时按原顺序合并。
