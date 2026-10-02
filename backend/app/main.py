"""FastAPI 入口：拓扑查询、阀门锁定、隔离方案计算、隔离演练工单。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, workorder
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import DrillWorkOrder, Valve
from .schemas import (
    IsolationIn,
    IsolationOut,
    TopologyOut,
    ValveLockIn,
    WorkOrderCreateIn,
    WorkOrderEventIn,
)
from .seed import reset_database, seed_database

@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用）",
    description="基于固定拓扑与阀门模型的检修隔离候选集合计算，不连接真实控制系统。",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    return TopologyOut(**isolation.topology_payload(db))


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    valve.locked = body.locked
    db.commit()
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    reset_database(db)
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    seed_database(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.locked = locked
        db.commit()

    try:
        payload = isolation.compute_isolation(db, body.target_id)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IsolationOut(**payload)


# ---------------------------------------------------------------- 隔离演练工单
# 工单只模拟流程：在冻结快照上维护模拟阀态，不写入真实阀门表、不连接真实
# 控制系统。每个事件都要求幂等标识 + 前置状态；约束变化即过期并阻止继续。


def _get_work_order_or_404(db: Session, wo_id: str) -> DrillWorkOrder:
    wo = db.get(DrillWorkOrder, wo_id)
    if wo is None:
        raise HTTPException(status_code=404, detail=f"工单不存在: {wo_id}")
    return wo


@app.post("/api/work-orders", status_code=201)
def create_work_order(body: WorkOrderCreateIn, db: Session = Depends(get_db)) -> dict:
    """由一次可行方案生成演练工单（草稿）：冻结目标、必要供给点、锁定状态、
    拓扑快照与所选方案，并生成 关阀→核验→复位 的计划步骤。"""
    seed_database(db)
    try:
        wo = workorder.create_work_order(
            db, target_id=body.target_id, close_valves=body.close_valves, title=body.title
        )
    except workorder.WorkOrderInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return workorder.detail_payload(db, wo)


@app.get("/api/work-orders")
def list_work_orders(db: Session = Depends(get_db)) -> list[dict]:
    seed_database(db)
    return workorder.list_payload(db)


@app.get("/api/work-orders/{wo_id}")
def get_work_order(wo_id: str, db: Session = Depends(get_db)) -> dict:
    """工单详情：冻结快照、计划步骤与证据、模拟阀态、NetworkX 复核结果、审计流。

    刷新页面后重新调用本接口即可完整重放工单状态与历史路径。"""
    wo = _get_work_order_or_404(db, wo_id)
    return workorder.detail_payload(db, wo)


@app.post("/api/work-orders/{wo_id}/events")
def post_work_order_event(wo_id: str, body: WorkOrderEventIn, db: Session = Depends(get_db)) -> dict:
    """提交工单事件（开始/模拟关阀/核验/模拟复位/暂停/恢复）。

    - 重复幂等标识 → 返回首次记录的结果（duplicate），不重复应用；
    - 乱序或前置状态不符 → 409 安全拒绝并落审计，不跳过任何步骤；
    - 锁定/拓扑/供给约束已变化 → 工单过期，409 阻止继续。
    """
    wo = _get_work_order_or_404(db, wo_id)
    try:
        return workorder.post_event(
            db,
            wo,
            action=body.action,
            valve_id=body.valve_id,
            idempotency_key=body.idempotency_key,
            expected=body.expected,
        )
    except workorder.WorkOrderInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except workorder.WorkOrderConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), **exc.detail}) from exc


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
