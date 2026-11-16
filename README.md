# 实时指标计算与监控大盘平台

面向实时指标计算引擎、多维度下钻分析、告警阈值管理、指标异常检测、数据服务开放与可视化看板配置的数据分析与应用工程平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实时指标 | `realtimemetric` | 指标定义 | 指标编号、指标名称、计算窗口秒 |
| 监控大盘 | `dashboard` | 大盘配置 | 大盘编号、大盘名称、包含指标组 |
| 多维下钻 | `drilldown` | 下钻路径 | 路径编号、起始维度、下钻层级序列 |
| 阈值管理 | `threshold` | 阈值规则 | 规则编号、关联指标、阈值下限 |
| 异常检测 | `anomalydetect` | 检测模型 | 模型编号、监控指标、检测算法 |
| 数据服务 | `dataservice` | 服务接口 | 服务编号、服务名称、接口路径 |
| 告警历史 | `alarmhistory` | 告警记录 | 记录编号、告警规则、触发时间 |
| 同比环比 | `comparison` | 对比配置 | 配置编号、目标指标、对比类型 |
| 聚合配置 | `aggregate` | 聚合规则 | 规则编号、源数据粒度、目标粒度 |
| 筛选预设 | `filterpreset` | 预设模板 | 预设编号、预设名称、维度条件组 |
| 用户洞察 | `userinsight` | 洞察报告 | 报告编号、分析主题、目标人群 |
| 事件分析 | `eventanalysis` | 分析模型 | 模型编号、事件名称、分组维度 |
| 分群画像 | `segmentprofile` | 画像定义 | 画像编号、分群名称、筛选规则 |
| 趋势预测 | `trendforecast` | 预测方案 | 方案编号、预测指标、算法模型 |
| 可视化模板 | `viztemplate` | 图表模板 | 模板编号、图表类型、数据绑定 |
| 订阅推送 | `subscription` | 订阅配置 | 订阅编号、订阅内容、接收人 |
| 维度映射 | `dimensionmap` | 映射规则 | 规则编号、原始维度名、标准维度名 |
| 数据质量分 | `qualityscore` | 评分规则 | 规则编号、数据集名称、完整性权重 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 用户洞察：增量刷新与快照追溯

### 数据模型

| 表 | 说明 |
| --- | --- |
| `_insight_crowds` | 目标人群及其版本；修改定义只升版本 |
| `_insight_batches` | 生成批次；每个批次覆盖一段日期，带 `已完成 / 进行中` 状态与计数 |
| `_insight_snapshots` | 报告数据快照：当前版本唯一，历史版本只读保留 |
| `_insight_scans` | 按 `(人群编号, 人群版本, 批次)` 共享的扫描缓存 |
| `_insight_status_logs` | 每次刷新的状态记录（动作、快照版本、区间、下次生成） |
| `_insight_deliveries` | 订阅推送消费快照的交付流水 |

内部表以 `_` 开头，不参与运营概览统计，但与业务表共用 `store.transaction()`：
快照、报告状态、扫描缓存与状态记录要么一起提交，要么整组回滚。

### 三个关键取舍

1. **修改人群不整份重算**：`POST /api/userinsight/crowds/{人群}/modify` 只升版本并
   把关联报告标记为 `待刷新`，历史指标、数据区间、生成时间与下次生成时间全部保持原样。
   之后的刷新按人群版本复用未受影响批次的扫描结果。
2. **只读取一个连续完整数据区间**：自动刷新在"更新但断档"与"较旧但连续"之间选择后者，
   同长度时取更晚的区间。被排除的批次会连同原因写进快照的 `区间判定`。
   示例数据里 `CROWD-0001` 的 `BATCH-0006` 已完成但与前段隔了 2 天缺口，因此
   `USER-0001/0002` 的区间停在 `2026-09-01 ~ 2026-09-08`。
3. **同一业务键只有一个当前快照**：报告编号即业务键，新版本写入前旧版本降为历史版本，
   版本号单调递增；`GET /{id}/snapshots` 保留全部历史原文。

### 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/userinsight/summary` | 报告数、待刷新数、当前快照数 |
| `GET /api/userinsight/crowds` | 人群与其批次全景（断档、未完成一目了然） |
| `POST /api/userinsight/{id}/refresh` | 增量刷新；可传 `batch_ids` 指定区间，不连续直接 400 |
| `POST /api/userinsight/batch-refresh` | 一次提交多个报告，逐条返回每个批次结果 |
| `GET /api/userinsight/{id}/snapshots/{version}` | 按版本读取历史快照 |
| `GET /api/userinsight/{id}/trace` | 状态记录、区间判定、扫描明细与订阅交付 |
| `POST /api/subscription/{id}/consume-insight` | 订阅消费当前快照，重复消费幂等拦下 |
| `GET /api/subscription/{id}/deliveries` | 交付流水 |

关键指标变化对比的是"上一当前快照"，仅当本期区间完整包含上一区间时才作为环比结论；
区间缩短或错位时只展示数值并在核心发现里注明不可比。

### 测试

```bash
cd backend
.venv/bin/python -m unittest discover -s tests -t . -v
```

领域：数据工程
子方向：数据分析与数据应用
