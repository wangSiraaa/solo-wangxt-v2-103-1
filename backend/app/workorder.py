"""隔离演练工单：把一次隔离计算冻结为可暂停、可核验、可恢复的模拟流程。

安全边界（与系统整体一致）：工单只模拟演练流程——所有“关阀/核验/复位”
都作用在工单自己冻结的拓扑快照上，绝不改动 Valve 表的真实状态，
也不连接任何真实控制系统，不构成真实检修隔离（LOTO）依据。

关键机制：

1. 冻结：创建时把目标、必要供给点、锁定状态、拓扑快照、所选方案连同
   指纹（fingerprint）一起持久化；指纹覆盖拓扑结构、阀门开闭/锁定/
   可操作、来源与必要供给点。
2. 状态机：draft → executing → pending_verification → restoring → closed，
   可 pause/resume；任何时刻冻结前提被改变即转为 stale（过期）并阻止继续。
3. 幂等与顺序：每个事件携带 idempotency_key 与前置状态 expected_status；
   相同 key 的重复提交幂等回放原结果（不重复执行步骤），乱序/跳步/前置
   状态不符一律拒绝；所有提交（含被拒绝的）都写入持久化审计事件表。
4. 复核：核验步骤与工单详情中的模拟视图都用 NetworkX 在“冻结快照 +
   已完成步骤”上重放计算，不读取当前数据库阀态。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .isolation import (
    TopologyError,
    _load,
    _preferred_source_path,
    _reachable_sources,
    build_graph,
    compute_isolation,
)
from .models import Valve, WorkOrder, WorkOrderEvent, WorkOrderStep

DISCLAIMER = (
    "本工单仅模拟演练流程：关阀/核验/复位均为培训模拟，不连接真实控制系统，"
    "不构成真实检修隔离（LOTO）依据。"
)

# ---------- 状态机 ----------
ST_DRAFT = "draft"  # 草稿
ST_EXECUTING = "executing"  # 执行中（模拟关阀）
ST_PAUSED = "paused"  # 暂停
ST_VERIFY = "pending_verification"  # 待核验
ST_RESTORING = "restoring"  # 恢复中（模拟复位）
ST_CLOSED = "closed"  # 关闭
ST_STALE = "stale"  # 已过期（冻结前提被改变，禁止继续）

TERMINAL = {ST_CLOSED, ST_STALE}
PAUSABLE = {ST_EXECUTING, ST_VERIFY, ST_RESTORING}

STEP_CLOSE = "close_valve"
STEP_VERIFY = "verify"
STEP_RESTORE = "restore_valve"

EVENT_TYPES = {"start", STEP_CLOSE, STEP_VERIFY, "pause", "resume", STEP_RESTORE}


class OrderError(RuntimeError):
    """业务参数错误 -> HTTP 400。"""


class ConflictError(RuntimeError):
    """状态/顺序/过期冲突 -> HTTP 409（拒绝前已写审计）。"""


# ---------- 冻结快照与指纹 ----------

def topology_snapshot(db: Session) -> dict[str, Any]:
    """抓取当前拓扑快照（与 isolation._load 相同的边结构，可直接喂给 build_graph）。"""
    nodes, edges = _load(db)
    return {
        "nodes": [
            {"id": n.id, "name": n.name, "kind": n.kind, "essential": n.essential}
            for n in nodes
        ],
        "edges": {eid: dict(e) for eid, e in edges.items()},
        "sources": sorted(n.id for n in nodes if n.kind == "source"),
        "essentials": sorted(n.id for n in nodes if n.essential),
    }


def fingerprint_of(snapshot: dict[str, Any]) -> str:
    """对影响隔离结论的要素取哈希：拓扑结构、阀门开闭/锁定/可操作、来源与必要供给点。"""
    material = {
        "nodes": sorted((n["id"], n["kind"], n["essential"]) for n in snapshot["nodes"]),
        "edges": sorted(
            (
                e["id"],
                e["u"],
                e["v"],
                e["is_bypass"],
                e["valve_id"],
                e["is_open"],
                e["locked"],
                e["operable"],
            )
            for e in snapshot["edges"].values()
        ),
        "sources": snapshot["sources"],
        "essentials": snapshot["essentials"],
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _live_fingerprint(db: Session) -> str:
    return fingerprint_of(topology_snapshot(db))


# ---------- 模拟视图（NetworkX 在冻结快照 + 已完成步骤上重放） ----------

def _simulated_closed(order: WorkOrder) -> set[str]:
    """按步骤顺序重放：关阀加入、复位移除，得到当前模拟关闭集合。"""
    closed: set[str] = set()
    for step in sorted(order.steps, key=lambda s: s.seq):
        if step.status != "done":
            continue
        if step.kind == STEP_CLOSE and step.valve_id:
            closed.add(step.valve_id)
        elif step.kind == STEP_RESTORE and step.valve_id:
            closed.discard(step.valve_id)
    return closed


def simulated_view(order: WorkOrder) -> dict[str, Any]:
    """依据工单冻结的拓扑快照与已完成步骤，重放当前模拟状态（NetworkX 复核）。

    核验前即可用于展示“目标是否已隔离、各必要供给点的来源路径”；
    恢复（resume）后同样依赖本视图，保证只依据冻结阀态与已完成步骤。
    """
    snap = json.loads(order.snapshot_json)
    edges = snap["edges"]
    sources = snap["sources"]
    essentials = snap["essentials"]
    target = order.target_id
    closed = _simulated_closed(order)

    g = build_graph(edges, closed)
    isolation_ok = not _reachable_sources(g, target, sources)
    open_bypass = {
        (e["u"], e["v"])
        for e in edges.values()
        if e["is_bypass"] and e["is_open"] and e["valve_id"] not in closed
    }
    supply_paths = {p: _preferred_source_path(g, p, sources, open_bypass) for p in essentials}
    supplied = sorted(p for p, path in supply_paths.items() if path)
    valve_states: dict[str, str] = {}
    for e in edges.values():
        vid = e["valve_id"]
        if not vid:
            continue
        if vid in closed:
            valve_states[vid] = "sim_closed"  # 本工单模拟关闭
        elif not e["is_open"]:
            valve_states[vid] = "closed"  # 冻结时即关闭
        else:
            valve_states[vid] = "open"
    return {
        "engine": "networkx",
        "basis": "frozen_snapshot+completed_steps",
        "simulated_closed_valves": sorted(closed),
        "isolation_ok": isolation_ok,
        "essentials_supplied": supplied,
        "essentials_all_supplied": len(supplied) == len(essentials),
        "supply_paths": supply_paths,
        "valve_states": valve_states,
    }


# ---------- 审计 ----------

def _record(
    db: Session,
    order: WorkOrder,
    ev_type: str,
    *,
    result: str,
    detail: str = "",
    key: str | None = None,
    expected: str | None = None,
    payload: Any = None,
) -> WorkOrderEvent:
    ev = WorkOrderEvent(
        order_id=order.id,
        seq=len(order.events) + 1,
        type=ev_type,
        idempotency_key=key,
        expected_status=expected,
        result=result,
        detail=detail,
        payload_json=json.dumps(payload, ensure_ascii=False) if payload is not None else None,
    )
    order.events.append(ev)
    db.flush()
    return ev


def _mark_stale(db: Session, order: WorkOrder) -> None:
    order.status = ST_STALE
    order.resume_status = None
    _record(
        db,
        order,
        "stale_marked",
        result="accepted",
        detail="锁定状态、拓扑或供给约束在执行中改变，工单过期，禁止继续沿用旧方案",
    )


def _ensure_fresh(db: Session, order: WorkOrder) -> None:
    """非终态工单在读取/变更前做活体指纹检查，前提被改变即落库为过期。"""
    if order.status not in TERMINAL and _live_fingerprint(db) != order.fingerprint:
        _mark_stale(db, order)
        db.commit()
        db.refresh(order)


# ---------- 创建工单 ----------

def _new_order_id(db: Session) -> str:
    while True:
        oid = "WO-" + uuid.uuid4().hex[:8].upper()
        if db.get(WorkOrder, oid) is None:
            return oid


def create_order(
    db: Session,
    target_id: str,
    locks: dict[str, bool] | None,
    solution_rank: int = 0,
    title: str | None = None,
) -> WorkOrder:
    """计算隔离方案并冻结为工单（草稿状态）。"""
    if locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in locks if vid not in known]
        if unknown:
            raise OrderError(f"未知阀门: {', '.join(unknown)}")
        for vid, locked in locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.locked = locked
        db.commit()

    try:
        result = compute_isolation(db, target_id)
    except TopologyError as exc:
        raise OrderError(str(exc)) from exc
    if not result["feasible"] or not result["solutions"]:
        reason = result.get("infeasible_reason") or "无可行方案"
        raise OrderError(f"目标 {target_id} 当前无法生成工单：{reason}")
    if solution_rank < 0 or solution_rank >= len(result["solutions"]):
        raise OrderError(
            f"方案序号 {solution_rank} 不存在（共 {len(result['solutions'])} 个等价方案）"
        )
    sol = result["solutions"][solution_rank]

    snapshot = topology_snapshot(db)
    plan = {
        "close_valves": sol["close_valves"],
        "size": sol["size"],
        "alternative_rank": sol["alternative_rank"],
        "closes_bypass_valves": sol.get("closes_bypass_valves", []),
        "supply_paths": sol.get("supply_paths", {}),
    }
    order = WorkOrder(
        id=_new_order_id(db),
        title=title or f"隔离演练工单-{target_id}",
        target_id=target_id,
        status=ST_DRAFT,
        resume_status=None,
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        plan_json=json.dumps(plan, ensure_ascii=False),
        fingerprint=fingerprint_of(snapshot),
        disclaimer=DISCLAIMER,
    )
    db.add(order)
    seq = 0
    for vid in sol["close_valves"]:
        seq += 1
        order.steps.append(WorkOrderStep(seq=seq, kind=STEP_CLOSE, valve_id=vid))
    seq += 1
    order.steps.append(WorkOrderStep(seq=seq, kind=STEP_VERIFY, valve_id=None))
    for vid in sol["close_valves"]:
        seq += 1
        order.steps.append(WorkOrderStep(seq=seq, kind=STEP_RESTORE, valve_id=vid))
    db.flush()
    _record(
        db,
        order,
        "create",
        result="accepted",
        detail="工单创建：冻结目标、必要供给点、锁定状态、拓扑快照与所选方案",
        payload={"plan": plan, "fingerprint": order.fingerprint},
    )
    db.commit()
    db.refresh(order)
    return order


# ---------- 事件处理（状态机 + 幂等 + 顺序 + 过期） ----------

def _next_pending(order: WorkOrder, kind: str) -> WorkOrderStep | None:
    for s in sorted(order.steps, key=lambda x: x.seq):
        if s.kind == kind and s.status == "pending":
            return s
    return None


def apply_event(
    db: Session,
    order: WorkOrder,
    ev_type: str,
    key: str | None,
    expected_status: str | None,
    valve_id: str | None,
    note: str | None,
) -> tuple[WorkOrderEvent, bool]:
    """处理一个工单事件，返回 (事件, 是否幂等重复)。

    任何冲突都先写审计再抛 ConflictError（-> 409）。
    """
    if ev_type not in EVENT_TYPES:
        raise OrderError(f"未知事件类型: {ev_type}")

    # 1) 幂等回放：同一工单内相同 key 的已接受事件直接返回原结果，
    #    重复提交不重复执行步骤，但留 duplicate 审计。
    if key:
        for ev in order.events:
            if ev.result == "accepted" and ev.idempotency_key == key:
                _record(
                    db,
                    order,
                    ev.type,
                    result="duplicate",
                    detail=f"幂等回放：与事件 #{ev.seq} 重复，未重复执行",
                    key=key,
                    expected=expected_status,
                    payload={"replay_of": ev.seq},
                )
                db.commit()
                return ev, True

    def reject(detail: str, payload: Any = None) -> None:
        _record(
            db,
            order,
            ev_type,
            result="rejected",
            detail=detail,
            key=key,
            expected=expected_status,
            payload=payload,
        )
        db.commit()
        raise ConflictError(detail)

    # 2) 过期检查：冻结前提（锁阀/拓扑/供给约束）在执行中改变 => 过期并阻止继续
    if order.status not in TERMINAL and _live_fingerprint(db) != order.fingerprint:
        _mark_stale(db, order)
        db.commit()
        reject("工单已过期：锁定、拓扑或供给约束被改变，禁止继续沿用旧方案，请重新计算并新建工单")
    if order.status == ST_STALE:
        reject("工单已过期，禁止继续")
    if order.status == ST_CLOSED:
        reject("工单已关闭，禁止继续")

    # 3) 前置状态校验（客户端必须声明它看到的前置状态）
    if expected_status != order.status:
        reject(f"前置状态不符：声明 {expected_status}，当前 {order.status}")

    # 4) 状态机与步骤顺序（乱序/跳步一律拒绝，不会跳过任何步骤）
    if ev_type == "start":
        if order.status != ST_DRAFT:
            reject(f"仅草稿状态可开始执行（当前 {order.status}）")
        order.status = ST_EXECUTING
        ev = _record(db, order, ev_type, result="accepted", detail="工单开始执行",
                     key=key, expected=expected_status, payload={"note": note})

    elif ev_type == STEP_CLOSE:
        if order.status != ST_EXECUTING:
            reject(f"当前状态 {order.status} 不能执行关阀步骤")
        step = _next_pending(order, STEP_CLOSE)
        if step is None:
            reject("没有待执行的关阀步骤")
        if valve_id != step.valve_id:
            reject(f"乱序提交：下一步应关闭 {step.valve_id}，收到 {valve_id}；不允许跳步")
        step.status = "done"
        view = simulated_view(order)
        step.evidence_json = json.dumps(
            {
                "action": f"模拟关闭 {valve_id}",
                "simulated_closed": view["simulated_closed_valves"],
                "isolation_ok": view["isolation_ok"],
                "note": note,
            },
            ensure_ascii=False,
        )
        if _next_pending(order, STEP_CLOSE) is None:
            order.status = ST_VERIFY
        ev = _record(db, order, ev_type, result="accepted",
                     detail=f"模拟关阀 {valve_id} 完成",
                     key=key, expected=expected_status,
                     payload={"valve_id": valve_id, "isolation_ok": view["isolation_ok"]})

    elif ev_type == STEP_VERIFY:
        if order.status != ST_VERIFY:
            reject(f"当前状态 {order.status} 不能提交核验（须先完成全部关阀步骤）")
        step = _next_pending(order, STEP_VERIFY)
        view = simulated_view(order)
        plan = json.loads(order.plan_json)
        planned = sorted(plan["close_valves"])
        ok = (
            view["isolation_ok"]
            and view["essentials_all_supplied"]
            and view["simulated_closed_valves"] == planned
        )
        evidence = {
            "engine": "networkx",
            "basis": "frozen_snapshot+completed_steps",
            "fingerprint": order.fingerprint,
            "planned_closed": planned,
            "simulated_closed": view["simulated_closed_valves"],
            "isolation_ok": view["isolation_ok"],
            "essentials_all_supplied": view["essentials_all_supplied"],
            "supply_paths": view["supply_paths"],
            "note": note,
        }
        if not ok:
            reject("NetworkX 复核未通过：目标未完全隔离或必要供给点断供", payload=evidence)
        step.status = "done"
        step.evidence_json = json.dumps(evidence, ensure_ascii=False)
        order.status = ST_RESTORING
        ev = _record(db, order, ev_type, result="accepted",
                     detail="核验通过：目标已隔离且全部必要供给点仍有来源（NetworkX 复核）",
                     key=key, expected=expected_status, payload=evidence)

    elif ev_type == "pause":
        if order.status not in PAUSABLE:
            reject(f"当前状态 {order.status} 不能暂停")
        order.resume_status = order.status
        order.status = ST_PAUSED
        ev = _record(db, order, ev_type, result="accepted",
                     detail=f"工单暂停（恢复后回到 {order.resume_status}）",
                     key=key, expected=expected_status,
                     payload={"resume_status": order.resume_status})

    elif ev_type == "resume":
        if order.status != ST_PAUSED:
            reject(f"当前状态 {order.status} 不能恢复")
        done_steps = [s.seq for s in order.steps if s.status == "done"]
        order.status = order.resume_status or ST_EXECUTING
        order.resume_status = None
        ev = _record(db, order, ev_type, result="accepted",
                     detail="依据工单冻结的阀态与已完成步骤恢复，中断前证据保留",
                     key=key, expected=expected_status,
                     payload={"resumed_to": order.status, "done_steps": done_steps})

    else:  # STEP_RESTORE
        if order.status != ST_RESTORING:
            reject(f"当前状态 {order.status} 不能执行复位步骤")
        step = _next_pending(order, STEP_RESTORE)
        if step is None:
            reject("没有待执行的复位步骤")
        if valve_id != step.valve_id:
            reject(f"乱序提交：下一步应复位 {step.valve_id}，收到 {valve_id}；不允许跳步")
        step.status = "done"
        view = simulated_view(order)
        step.evidence_json = json.dumps(
            {
                "action": f"模拟复位 {valve_id}",
                "simulated_closed": view["simulated_closed_valves"],
                "note": note,
            },
            ensure_ascii=False,
        )
        if _next_pending(order, STEP_RESTORE) is None:
            order.status = ST_CLOSED
        ev = _record(db, order, ev_type, result="accepted",
                     detail=f"模拟复位 {valve_id} 完成",
                     key=key, expected=expected_status,
                     payload={"valve_id": valve_id,
                              "simulated_closed": view["simulated_closed_valves"]})

    db.commit()
    return ev, False


# ---------- 序列化 ----------

def order_summary(db: Session, order: WorkOrder) -> dict[str, Any]:
    _ensure_fresh(db, order)
    plan = json.loads(order.plan_json)
    return {
        "id": order.id,
        "title": order.title,
        "target_id": order.target_id,
        "status": order.status,
        "plan_close_valves": plan["close_valves"],
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "disclaimer": order.disclaimer,
    }


def order_payload(db: Session, order: WorkOrder, *, check_stale: bool = True) -> dict[str, Any]:
    if check_stale:
        _ensure_fresh(db, order)
    snap = json.loads(order.snapshot_json)
    plan = json.loads(order.plan_json)
    return {
        "id": order.id,
        "title": order.title,
        "target_id": order.target_id,
        "status": order.status,
        "resume_status": order.resume_status,
        "disclaimer": order.disclaimer,
        "fingerprint": order.fingerprint,
        "plan": plan,
        "snapshot": {
            "sources": snap["sources"],
            "essentials": snap["essentials"],
            "nodes": snap["nodes"],
            "valves": [
                {
                    "id": e["valve_id"],
                    "name": e["valve_name"],
                    "segment_id": e["id"],
                    "is_open": e["is_open"],
                    "locked": e["locked"],
                    "operable": e["operable"],
                }
                for e in snap["edges"].values()
                if e["valve_id"]
            ],
        },
        "steps": [
            {
                "seq": s.seq,
                "kind": s.kind,
                "valve_id": s.valve_id,
                "status": s.status,
                "evidence": json.loads(s.evidence_json) if s.evidence_json else None,
            }
            for s in sorted(order.steps, key=lambda x: x.seq)
        ],
        "events": [
            {
                "seq": e.seq,
                "type": e.type,
                "result": e.result,
                "detail": e.detail,
                "idempotency_key": e.idempotency_key,
                "expected_status": e.expected_status,
                "payload": json.loads(e.payload_json) if e.payload_json else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in sorted(order.events, key=lambda x: x.seq)
        ],
        "simulation": simulated_view(order),
        "created_at": order.created_at.isoformat() if order.created_at else None,
        "updated_at": order.updated_at.isoformat() if order.updated_at else None,
    }
