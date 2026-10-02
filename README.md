# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合，并可把一次计算冻结为**可暂停、可核验、可恢复的隔离演练工单**。
**不连接任何真实控制系统，计算结果与工单流程不代表真实检修已满足安全隔离条件。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案与残余路径高亮、
  工单步骤与模拟供给路径展示）
- 后端：FastAPI + NetworkX（无向物理连通图上的候选阀门集合枚举与约束校验、
  工单冻结快照上的模拟状态重放与核验复核）
- 存储：PostgreSQL（节点连接、阀门开闭/锁定、必要供给点、工单/步骤/审计事件持久化；
  本地无 PG 时自动回退 SQLite）

## 演示拓扑

```
SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
          │            ╲  旁路 N2─V_BP_IN─BP─V_BP_OUT─N3 ╱
         V_P1           环网联络 N1─V_LK1─N5─V_LK2─N3
          ↓                          N3 ─V_P2─ P2
          P1
```

- 每条管段显式保存名义方向（upstream→downstream，图上箭头标注）；隔离按**无向物理连通**计算。
- 旁路（紫虚线）与目标设备 T 并联；P1、P2 为**必要供给点**（任何方案不得断供）。
- 阀门与管段 1:1；初始全部打开。锁定 = 禁止关闭（保持现状），可在页面勾选后重新计算。

## 三个培训样例

| 样例 | 锁定 | 结果 |
| --- | --- | --- |
| 1 旁路绕回 | 无 | 关 `V_TIN`+`V_TOUT` 即隔离 T；旁路保持打开，P2 经 `N2→BP→N3` 绕回不断供 |
| 2 锁定入口阀 | `V_TIN` | 最小集合升为 3 阀：`V_TOUT`+`V1`+旁路一只（`V_BP_IN`/`V_BP_OUT` 两个等价方案）；旁路被封，P2 改由环网 `N1→N5→N3` 供料 |
| 3 不应断供的支路 | `V_TOUT` | **无可行方案**：任何切法都不可避免断供 P2（P1 可保住）；页面显示仍连通的残余路径 `SRC→N1→N2→T`，以及经锁定阀的见证路径 `…→N3→T`（含 `V_TOUT`） |

> 仅关 T 一侧阀门时隔离不成立：例如只关 `V_TIN`，介质仍可经旁路绕回 N3 再回到 T
> （`N2→BP→N3→T`），或经环网绕回。这正是样例 1 必须两侧同关的原因。

## 隔离演练工单

把一次可行计算**冻结**为一张演练工单，按步骤模拟“关阀 → 核验 → 复位”的完整演练，
而不是只看一张结果图。**工单只模拟流程，不连接真实控制系统**（每张工单都携带该声明）。

- **冻结**：创建时持久化目标、必要供给点、锁定状态、拓扑快照、所选方案，
  并计算前提指纹（拓扑结构 + 阀门开闭/锁定/可操作 + 来源/必要供给点的 SHA-256）。
- **状态机**：`草稿 → 执行中 → 待核验 → 恢复中 → 关闭`，可 `暂停/恢复`；
  任何时刻锁定、拓扑或供给约束被改变，工单即转为 `已过期` 并**阻止继续**，
  不会沿用旧方案产生部分假完成。
- **幂等与顺序**：每个事件携带 `idempotency_key` 与前置状态 `expected_status`；
  相同 key 的重复提交幂等回放原结果（不重复执行步骤），乱序/跳步/前置状态不符
  一律 `409` 拒绝；**所有提交（含被拒绝与重复的）都写入持久化审计事件表**。
- **核验与重放**：核验步骤与工单详情的模拟视图都用 NetworkX 在
  “冻结快照 + 已完成步骤”上重放计算——核验前即可看到目标是否已隔离、
  各必要供给点的来源路径；暂停/刷新/恢复后按同一冻结依据继续，中断前证据保留。
- **模拟边界**：工单演练绝不改动 `valves` 表的真实状态，复位只作用于工单自己的快照。

### 工单 API

- `POST /api/workorders`：body `{ "target_id": "T", "locks": {...}, "solution_rank": 0 }`
  → 计算可行方案并冻结为工单（草稿）；无可行方案返回 400
- `POST /api/workorders/{id}/events`：提交事件，body
  `{ "type": "start|close_valve|verify|pause|resume|restore_valve",
     "idempotency_key": "...", "expected_status": "...", "valve_id": "...", "note": "..." }`
  → 接受返回工单详情；冲突（过期/乱序/前置状态不符）返回 409 且已写审计
- `GET /api/workorders/{id}`：工单详情（冻结快照、计划步骤、模拟视图、审计事件）；
  读取时做活体指纹检查，前提被改变即落库为过期
- `GET /api/workorders`：工单列表（含免责声明）

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# API: http://127.0.0.1:8000/api/topology, /api/isolation, /api/valves/{id}/lock, /api/reset
# 测试: python3 -m pytest tests/ -q
```

指定 PostgreSQL：

```bash
export DATABASE_URL=postgresql+psycopg://isolation:isolation@localhost:5432/isolation_demo
docker compose up -d db        # 或使用任意已有 PG 实例
```

### 前端

```bash
cd frontend
npm install
npm start                      # http://localhost:4200 （/api 代理到 8000）
# 或产物构建后由后端直接托管: npx ng build  → http://127.0.0.1:8000/
```

### 一键（含 PG）

```bash
cd frontend && npm ci && npx ng build && cd ..
docker compose up --build
```

## API 摘要

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`）
- `POST /api/isolation`：body `{ "target_id": "T", "locks": {"V_TIN": true} }`
  - 可行：`best_solution`（最少阀门）、等价方案、方案后每个必要供给点的来源路径
  - 不可行：`residual_path`（仍连通的一条残余路径）、`locked_witness_path`（经锁定阀的见证路径）、
    `unconstrained_best.unavoidable_essentials`（任何切法都无法保住的供给点）
- `POST /api/valves/{valve_id}/lock`：持久化单只阀门锁定状态
- `POST /api/reset`：全部阀门恢复打开、未锁定
- `POST /api/workorders` / `GET /api/workorders` / `GET /api/workorders/{id}` /
  `POST /api/workorders/{id}/events`：隔离演练工单（见上节）

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举演示：假设阀门与管段 1:1、关阀即断边、
无背压/泄漏/盲板/双阀双断等真实工况要素。隔离演练工单同样只在冻结快照上
模拟流程，不改动真实阀门状态、不连接任何真实控制系统。任何输出**不得**作为
真实检修隔离（LOTO）的安全依据。
