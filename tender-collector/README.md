# 市政工程投标截止时间采集服务

面向招投标专员的公告采集 API：提交市政工程公告站的**列表页地址**，服务自动遍历
所有分页与详情页，将**更正公告按其引用的原公告编号归并**到同一项目，并**按发布
时间**（而非页面排列顺序）确定当前生效的投标截止时间，标记是否已过期。

采集的同时为每条公告保存**追溯证据**（截止时间原文、详情链接、字段所在表格行、
详情 HTML 的 SHA-256），资料员复核截标时间时可按原公告编号调用**追溯 API**，
逐条查看原公告及各次更正的原文依据，确认哪次更正使当前查询值生效。

## 核心规则

1. **分页遍历**：从提交的列表页出发，沿 `nav.pagination` 链接抓取全部列表页
   （去重、防环、限量），收集所有详情页链接后逐页抓取。
2. **更正归并**：更正公告通过"原公告编号"归并到对应原公告所在项目；引用悬空
   （原公告不存在/不是原公告）视为数据错误，本次采集整体失败。
3. **生效截止时间**：同一项目内按**发布时间**排序，取发布时间最新且声明了
   截止时间的公告；发布时间相同时按公告编号确定次序，结果与页面顺序完全无关。
   若最新更正公告未给出截止时间（例如只改联系人），沿用上一份带截止时间的公告。
4. **过期判定**：`is_expired = 截止时间 <= 当前时间`（截止时间按服务器本地时区
   的 naive 时间比较，与公告页 `YYYY-MM-DD HH:MM` 格式一致）。
5. **幂等采集**：公告以编号为唯一键 UPSERT，重复采集不产生重复记录。
6. **证据同事务**：采集成功时，运行记录、公告记录与追溯证据（截止时间原文、
   字段所在表格行、详情 HTML 的 SHA-256、来源轮次）在**同一事务**写入；
   同一详情复采后字段变化时，公告与证据原子切换到新一轮内容，
   项目查询与追溯查询永远对应同一轮采集结果。
7. **失败保留**：任一列表页/详情页读取失败（网络错误、非 2xx、字段缺失）或
   归并校验失败时，本次采集不写入任何公告与证据数据，**完整保留上次成功采集
   结果**；失败原因记入 `collection_runs` 表并通过 HTTP 422 返回。

## 目录结构

```
tender-collector/
├── app/                    # FastAPI 应用
│   ├── main.py             # 路由：/api/collect、/api/projects、
│   │                       #       /api/traceability/{code}、/api/collections
│   ├── service.py          # 采集编排 + 事务边界 + 过期判定 + 追溯查询
│   ├── scraper.py          # httpx + BeautifulSoup 抓取/解析 + 证据提取
│   ├── merge.py            # 更正归并与生效截止时间判定（纯函数）
│   ├── database.py         # SQLite 连接、建表、UPSERT（公告 + 证据）
│   ├── models.py           # Pydantic 模型
│   └── config.py           # 环境变量配置
├── demo_site/              # 第一轮示例公告站（静态文件）
├── demo_site_round2/       # 第二轮示例站：同一批页面，部分详情字段已变化
├── scripts/generate_demo_site.py  # 示例站生成器（两轮数据均可复现）
├── tests/                  # 端到端 + 归并 + 追溯测试
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
# 1. 生成示例公告站静态文件（已随仓库提供，可重复生成）
python3 scripts/generate_demo_site.py

# 2. 启动：API 在 :8000，示例公告站在 :8001（第二轮样例挂在 /round2/）
docker compose up --build -d

# 3. 提交列表页地址触发采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/list_1.html"}'

# 4. 查询项目当前截止时间
curl http://localhost:8000/api/projects

# 5. 按原公告编号追溯生效依据
curl http://localhost:8000/api/traceability/SZ-2026-002

# 6. 模拟源站改稿后复采：第二轮样例中两个详情页的截止时间已变化，
#    复采后项目查询与追溯证据同步切换到新一轮内容
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://demo-site/round2/list_1.html"}'
```

容器内 API 通过 compose 网络访问示例站（主机名 `demo-site`）；从宿主机浏览
示例站则打开 <http://localhost:8001/list_1.html>（第二轮：
<http://localhost:8001/round2/list_1.html>）。SQLite 数据存于命名卷
`tender-data`（容器内 `/data/tenders.db`）。

停止与清理：

```bash
docker compose down          # 停止（保留数据卷）
docker compose down -v       # 停止并删除采集数据
```

## 本地开发运行（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 scripts/generate_demo_site.py

# 终端 A：示例公告站
python3 -m http.server 8001 --directory demo_site

# 终端 B：API（数据库默认在 ./data/tenders.db）
uvicorn app.main:app --reload --port 8000

# 终端 C：测试
pytest -q

# 采集
curl -X POST http://localhost:8000/api/collect \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://127.0.0.1:8001/list_1.html"}'
```

交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>

## API 说明

### `POST /api/collect` — 提交列表页，触发采集

请求：

```json
{ "list_url": "http://demo-site/list_1.html" }
```

成功响应（200）：

```json
{
  "status": "success",
  "source_url": "http://demo-site/list_1.html",
  "collected": 14,
  "projects": [
    {
      "project_name": "城北污水处理厂扩建工程",
      "current_deadline": "2026-11-30 10:00",
      "deadline_source_code": "SZ-2026-002-G2",
      "deadline_source_url": "http://demo-site/SZ-2026-002-G2.html",
      "is_expired": false
    }
  ]
}
```

失败响应（422，任一页面不可读/数据无法归并）：

```json
{ "detail": "读取页面失败 http://demo-site/SZ-2026-002-G2.html: HTTP 404" }
```

### `GET /api/projects` — 查询全部项目的当前截止时间

返回字段：项目名称、当前生效截止时间、截止时间来源公告编号、来源链接、是否过期。

### `GET /api/traceability/{code}` — 按原公告编号追溯生效依据

输入**已采集的原公告编号**，按发布时间升序列出原公告及各次更正的追溯证据，
标明哪一份公告是当前查询值的生效依据；未写截止时间的更正标注沿用来源。

响应示例（节选）：

```json
{
  "original_code": "SZ-2026-006",
  "project_name": "滨江大道地下综合管廊工程",
  "current_deadline": "2026-12-05 14:00",
  "effective_code": "SZ-2026-006-G3",
  "effective_url": "http://demo-site/SZ-2026-006-G3.html",
  "is_expired": false,
  "entries": [
    {
      "code": "SZ-2026-006",
      "kind": "original",
      "published_at": "2026-10-15 09:30",
      "deadline_text": "2026-11-12 10:00",
      "deadline_row": 5,
      "detail_url": "http://demo-site/SZ-2026-006.html",
      "detail_sha256": "67aacfa2…",
      "collected_run_id": 1,
      "is_effective": false,
      "note": "其截止时间已被后续公告 SZ-2026-006-G3 取代，未生效"
    },
    {
      "code": "SZ-2026-006-G2",
      "deadline_text": null,
      "deadline_row": null,
      "is_effective": false,
      "note": "未声明投标截止时间，沿用 SZ-2026-006-G1 的截止时间"
    },
    {
      "code": "SZ-2026-006-G3",
      "deadline_text": "2026-12-05 14:00",
      "is_effective": true,
      "note": "生效依据：发布时间最新且声明投标截止时间的公告，当前查询值以此为准"
    }
  ]
}
```

每条 `entries` 字段说明：

| 字段 | 含义 |
| --- | --- |
| `deadline_text` | 投标截止时间**原文**（详情页表格单元原始文本；未声明则为 `null`） |
| `deadline_row` | “投标截止时间”字段所在表格行（`table.detail-table` 内 `<tr>` 从 1 数起） |
| `detail_url` | 详情链接 |
| `detail_sha256` | 采集时详情 HTML **原始字节**的 SHA-256 |
| `collected_run_id` | 证据来源的采集轮次 id（对应 `collection_runs.id`） |
| `is_effective` / `note` | 是否为生效依据 / 生效、取代或沿用说明 |

错误响应：编号不存在 → 404；编号属于更正公告 → 400（提示改用其原公告编号）。

### `GET /api/collections?limit=20` — 采集运行历史

包含每次采集的成功/失败状态、采集条数与错误信息。

## 示例站数据设计

`demo_site/` 含 6 个项目、14 条公告、4 个列表页，列表按发布时间**倒序**排列
（更正公告排在原公告之前），专门用于验证归并不受页面顺序影响：

| 项目 | 场景 | 生效截止时间来源 |
| --- | --- | --- |
| 滨河路市政改造工程 | 一次更正延期 | 更正公告 SZ-2026-001-G1 |
| 城北污水处理厂扩建工程 | 两次更正，取最新一次 | 第二次更正 SZ-2026-002-G2 |
| 东风桥加固维修工程 | 无更正，已过期 | 原公告 SZ-2026-003 |
| 高新区雨污分流管网工程 | 更正不改截止时间 | 原公告 SZ-2026-004 |
| 湖滨公园配套市政道路工程 | 原公告在末页、更正在首页（跨页乱序） | 更正公告 SZ-2026-005-G1 |
| 滨江大道地下综合管廊工程 | 连续三次更正，第二次未写截止时间（沿用第一次） | 第三次更正 SZ-2026-006-G3 |

`demo_site_round2/` 模拟**同一详情页复采时字段变化**：页面结构与第一轮相同，
但 `SZ-2026-001-G1`（→ 2026-12-02 14:30）与 `SZ-2026-006-G3`
（→ 2026-12-08 09:00）的投标截止时间被源站再次调整。对第二轮地址复采后，
项目查询与追溯证据（截止时间原文、SHA-256、轮次 id）同步切换到新一轮内容。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TENDER_DB_PATH` | 本地 `./data/tenders.db`；容器内 `/data/tenders.db` | SQLite 文件路径 |
| `TENDER_HTTP_TIMEOUT` | `15` | 单个页面请求超时（秒） |
| `TENDER_MAX_LIST_PAGES` | `200` | 单次采集最多遍历的列表页数（防环） |
| `TENDER_USER_AGENT` | `TenderCollector/1.0 ...` | 请求 UA |

## 解析约定（接入真实站点时）

解析器依赖以下稳定选择器，适配其他站点时调整 `app/scraper.py` 即可：

- 列表页：`ul.announcement-list a.detail-link` 为详情链接，
  `nav.pagination a` 为分页链接；
- 详情页：`article.announcement-detail` 容器，支持
  `data-code` / `data-kind` / `data-published-at` / `data-corrects` 属性，
  以及 `table.detail-table` 内 `th[data-field="字段名"] + td` 表格字段
  （公告编号、公告类型、项目名称、发布时间、投标截止时间、原公告编号）；
- 追溯证据：`投标截止时间` 字段的单元文本与其在 `table.detail-table` 内的
  行号（`<tr>` 从 1 数起）在解析时一并提取，SHA-256 按响应原始字节计算。
