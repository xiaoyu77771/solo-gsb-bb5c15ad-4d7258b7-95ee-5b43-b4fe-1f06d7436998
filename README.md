# 维护窗口排程 Web 应用（Maintenance Window Scheduler）

在指定维护窗口内，对带**服务依赖 DAG、维护时长、资源占用、禁排时间段**的一批维护任务自动求解：
计算满足依赖与资源容量约束的**可执行顺序和开始/结束时间**，检测**循环依赖**与**资源重叠**，
给出**可定位的冲突链**；甘特图上可拖动调整窗口并即时重算，支持场景保存/恢复与 JSON 导出。

- 后端：FastAPI + SQLite（标准库 `sqlite3`，零额外 ORM 依赖）
- 前端：原生 HTML / CSS / JavaScript（Canvas 绘制甘特图，无任何前端构建步骤）
- 部署：单镜像 Docker / docker compose 一键启动

---

## 一、快速开始

### 方式 A：docker compose（推荐，一键）

```bash
docker compose up -d --build
```

启动后浏览器访问：

> **http://localhost:8000**

停止：

```bash
docker compose down            # 保留数据（命名卷 scheduler-data）
docker compose down -v         # 同时删除保存的场景数据
```

### 方式 B：单条 docker 命令

```bash
docker build -t maintenance-scheduler .
docker run -d --name mw \
  -p 8000:8000 \
  -v mw-data:/data \
  maintenance-scheduler
```

### 方式 C：本地直接运行（不使用 Docker）

需要 Python 3.11+：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 默认 SQLite 数据库：./data/maintenance.db（可用 MW_DATA_DIR 指定目录）
```

> 健康检查：`GET http://localhost:8000/api/health`
> 交互式 API 文档（Swagger）：`http://localhost:8000/docs`

---

## 二、访问端口与配置

| 项 | 默认值 | 说明 / 环境变量 |
| --- | --- | --- |
| Web 访问端口 | `8000` | compose 通过 `APP_PORT` 可改：`APP_PORT=9000 docker compose up -d --build` |
| 数据目录 | `/data` | `MW_DATA_DIR`：SQLite 数据库所在目录（容器内已挂卷持久化） |
| 数据库文件 | `<数据目录>/maintenance.db` | `MW_DB_PATH`：直接指定数据库文件完整路径 |

修改宿主映射端口示例：

```bash
APP_PORT=9000 docker compose up -d --build   # 访问 http://localhost:9000
```

数据持久化：compose 使用命名卷 `scheduler-data` 挂载到容器 `/data`；
如希望数据库落在宿主目录，可把 `docker-compose.yml` 中的卷改为 `./data:/data`。

---

## 三、功能说明

### 1. 数据录入（左侧编辑面板）

- **维护窗口**：整体允许执行维护的开始/结束时间（`datetime-local`，5 分钟粒度）。
- **资源容量**：任意命名的共享资源（如 `DBA`、`网络组`、`应用组`）及容量上限（整数/小数）。
- **维护任务**：名称、时长（分钟）、对每种资源的占用量、可选的**固定开始时间**。
  - 不填固定开始时间 → 任务为**弹性**，由算法在窗口内寻找最早可行时刻；
  - 填写固定开始时间 → 任务**锁定**到该时刻，系统校验它是否越窗、违反依赖/禁排期/资源约束。
- **依赖关系**：`前置 → 后继`，表示前置完成后后继才能开始；多条边构成 DAG。
- **不可维护时间段（禁排期）**：命名时间区间，任何任务都不得与之重叠（如计费批处理窗口）。

页面右上角「**示例**」按钮会载入一个内置夜间窗口示例，便于快速体验。

### 2. 自动排程（右侧结果面板）

- 顶部状态栏：可行性、已排/总数、冲突数、总跨度（makespan）。
- **甘特图（Canvas）**：
  - 蓝色条＝弹性排程，紫色条 🔒＝固定开始时间；红色底纹为禁排期；红色虚线占位＝未排程任务。
  - 条间贝塞尔箭头表示依赖关系，横轴为时间刻度。
- **执行顺序表**：按开始时间排序的执行清单（开始/结束、时长、前置、资源占用）。

**冲突链可定位**：所有告警按类型列出，点击任意一条，甘特图上相关任务会红框高亮、
相关依赖边变红，并在冲突文字中显示链路，例如 `A → B → C`。

检测的冲突类型：

| 类型 | 含义 |
| --- | --- |
| `循环依赖 cycle` | Tarjan SCC 找出所有环，给出环上节点与闭环边（含自环），环上任务全部不排程 |
| `悬空依赖 dangling_edge` | 依赖边引用了已删除/不存在的任务 |
| `无法排程 unschedulable` | 受资源容量、禁排期挤压或窗口长度限制，窗口内放不下 |
| `依赖阻塞 dependency_blocked` | 前置任务未能排程，后继无法确定开始时间（沿依赖链传播） |
| `固定越界 fixed_window` | 固定任务超出维护窗口 |
| `固定冲突 fixed_predecessor` | 固定开始时间早于前置最早完成时间 |
| `禁排冲突 fixed_blackout` | 固定任务与禁排期重叠 |
| `资源冲突 fixed_resource` / `容量超限 fixed_capacity` | 固定时刻资源超容量，或单任务占用超过资源总容量 |

### 3. 拖动调整后即时重算

- 鼠标**拖动甘特条整体移动**：按 5 分钟吸附改变开始时间，任务随即变为"固定"，
  自动触发防抖重算（约 120–180ms），右侧结果即时更新；
- **拖动条右边缘**：调整任务时长后即时重算；
- **双击甘特条**：解除固定，恢复为弹性排程；
- 勾选框「拖动后即时重算」可关闭自动模式，改用「▶ 立即排程」手动触发。

### 4. 场景保存 / 恢复

- 输入场景名称 →「💾 保存场景」：同名场景执行**更新（upsert）**，异名新建。
- 下拉框选择历史场景 →「↺ 恢复」：完整还原窗口、资源、任务、依赖、禁排期并重算。
- 「🗑」删除当前选中场景。所有场景持久化在 SQLite `scenarios` 表。

### 5. JSON 导出

「⬇ 导出 JSON」会下载 `maintenance-schedule-<时间戳>.json`，内容同时包含：

```json
{
  "exported_at": "导出时间(UTC)",
  "scenario": { "…当前编辑场景的完整输入…" },
  "result":   { "feasible": true, "items": [ …排程明细… ], "order": [],
                "unscheduled": [], "conflicts": [], "stats": {}, "window": {} }
}
```

---

## 四、排程算法说明

1. **输入规整与校验**：时间格式、时长、资源用量、id 唯一性、区间合法性。
2. **环检测**：Tarjan 强连通分量找出所有循环依赖（含自环），还原可读环路径；
   环上任务标记不可排程，环外任务照常求解。
3. **拓扑排序**：Kahn 算法得到可执行顺序（固定时间任务优先，其余按 id 稳定排序）。
4. **贪心最早可行槽位**（按拓扑序）：
   - 候选起点 = max(窗口起点, 所有前置完成时刻)；
   - 若与禁排期重叠 → 跳到禁排期结束；
   - 事件点扫描检查每种资源的并发占用是否超容量 → 跳到冲突占用的最早释放时刻；
   - 重复直到找到可行起点；超出窗口终点或单任务需求 > 资源总容量则判定不可排程。
5. **固定时间任务**不参与寻找，直接在指定时刻逐项做窗口/依赖/禁排期/资源校验。
6. 不可排程沿出边传播为"依赖阻塞"，并输出可定位的冲突链。

> 注：该贪心（列表调度类）对"找到满足全部约束的可行计划"是完备的：
> 单任务需求不超容量、窗口确实放得下时，推迟式求解一定能排下；放不下时给出具体挤压来源。

---

## 五、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/schedule` | 提交场景 JSON，返回排程结果与冲突链 |
| POST | `/api/export` | 同 schedule，结果以 JSON 文件下载 |
| GET | `/api/scenarios` | 场景列表（id/名称/时间戳） |
| POST | `/api/scenarios` | 新建或按同名更新场景 `{name, data}` |
| GET | `/api/scenarios/{id}` | 恢复某场景（含完整 data） |
| PUT | `/api/scenarios/{id}` | 更新场景 |
| DELETE | `/api/scenarios/{id}` | 删除场景 |
| GET | `/api/health` | 健康检查 |

排程请求体示例：

```json
{
  "window_start": "2026-09-23T22:00",
  "window_end":   "2026-09-24T06:00",
  "resources": { "DBA": 1, "网络组": 1 },
  "nodes": [
    { "id": "a", "name": "主备切换", "duration_minutes": 30,
      "resources": { "DBA": 1 }, "fixed_start": null },
    { "id": "b", "name": "版本升级", "duration_minutes": 90,
      "resources": { "DBA": 1 }, "fixed_start": "2026-09-24T01:00" }
  ],
  "edges": [ { "source": "a", "target": "b" } ],
  "blackouts": [
    { "name": "计费批处理", "start": "2026-09-24T00:00", "end": "2026-09-24T00:30" }
  ]
}
```

---

## 六、目录结构与测试

```
.
├── app/
│   ├── main.py            # FastAPI：API、SQLite、静态托管、导出
│   ├── scheduler.py       # DAG 环检测 / 拓扑排序 / 资源+禁排期排程
│   ├── models.py          # Pydantic 模型
│   └── static/
│       ├── index.html     # 编辑器 + 结果区布局
│       ├── app.js         # 原生 JS：表格编辑、Canvas 甘特图、拖拽、场景、导出
│       └── styles.css
├── test_scheduler.py      # 排程核心测试（53 项断言，无需第三方库）
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

运行后端测试：

```bash
pip install -r requirements.txt   # 测试仅依赖 fastapi（含 pydantic）
python test_scheduler.py
```

---

## 七、常见问题

- **端口被占用**：`APP_PORT=9000 docker compose up -d --build`。
- **改了依赖镜像不生效**：`docker compose build --no-cache`。
- **时间按什么时区？** 前端使用浏览器本地时间生成 `datetime-local`，后端按朴素时间存储与计算，
  不做时区换算；容器与使用者在同一时区语义下即可。
- **资源容量能是小数吗？** 可以，如某资源 0.5 的占用也支持。
