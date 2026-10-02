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
from .models import Valve, WorkOrder
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


# ---------------- 隔离演练工单（仅模拟流程，不连接真实控制系统） ----------------

def _get_order_or_404(db: Session, order_id: str) -> WorkOrder:
    order = db.get(WorkOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail=f"工单不存在: {order_id}")
    return order


@app.post("/api/workorders", status_code=201)
def create_work_order(body: WorkOrderCreateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    """把一次可行隔离计算冻结为演练工单（草稿）。"""
    seed_database(db)
    try:
        order = workorder.create_order(
            db, body.target_id, body.locks, body.solution_rank, body.title
        )
    except workorder.OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return workorder.order_payload(db, order, check_stale=False)


@app.get("/api/workorders")
def list_work_orders(db: Session = Depends(get_db)) -> dict[str, object]:
    seed_database(db)
    orders = db.scalars(select(WorkOrder).order_by(WorkOrder.created_at.desc())).all()
    return {
        "disclaimer": workorder.DISCLAIMER,
        "orders": [workorder.order_summary(db, o) for o in orders],
    }


@app.get("/api/workorders/{order_id}")
def get_work_order(order_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    """工单详情：冻结快照、计划步骤、模拟视图（NetworkX 重放）、审计事件。

    读取时做活体指纹检查：锁定/拓扑/供给约束若在执行中被改变，
    工单在此落库为过期（stale），刷新页面即可看到。
    """
    order = _get_order_or_404(db, order_id)
    return workorder.order_payload(db, order)


@app.post("/api/workorders/{order_id}/events")
def post_work_order_event(
    order_id: str, body: WorkOrderEventIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    """提交工单事件（幂等 + 前置状态 + 顺序校验；全部提交均写审计）。"""
    order = _get_order_or_404(db, order_id)
    try:
        ev, duplicate = workorder.apply_event(
            db,
            order,
            body.type,
            body.idempotency_key,
            body.expected_status,
            body.valve_id,
            body.note,
        )
    except workorder.OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except workorder.ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    payload = workorder.order_payload(db, order, check_stale=False)
    payload["last_event"] = {"seq": ev.seq, "result": ev.result, "duplicate": duplicate}
    return payload


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
