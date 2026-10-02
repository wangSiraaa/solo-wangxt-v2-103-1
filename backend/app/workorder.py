"""隔离演练工单：把一次计算出的隔离方案冻结为可暂停、可核验、可恢复的模拟流程。

安全边界（与系统其余部分一致）：
- 工单在创建时冻结目标、必要供给点、锁定状态、拓扑快照与所选方案，
  之后只在快照上维护“模拟阀态”，全程不写入真实阀门表、不连接任何真实
  控制系统；核验（verify）用 NetworkX 在冻结快照 + 模拟阀态上复核。
- 每个事件必须携带幂等标识与前置状态：重复提交按幂等重放处理，乱序或
  前置状态不符被安全拒绝并落审计，绝不跳过计划步骤。
- 执行中锁定、拓扑或供给约束一旦变化（指纹不符），工单立即过期并阻止
  继续，不允许沿用旧方案；恢复（复位）只依据工单冻结的阀态与已完成步骤。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import networkx as nx
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation
from .models import DrillEvent, DrillStep, DrillWorkOrder, Node, Segment, Valve

# 状态机：
#   draft ──start──▶ executing ──全部关阀──▶ pending_verification ──verify──▶
#   restoring ──全部复位──▶ closed
#   executing / pending_verification / restoring ⇄ paused
#   任何非终态 + 约束指纹变化 ──▶ expired（终态，阻止继续）
STATUS_DRAFT = "draft"
STATUS_EXECUTING = "executing"
STATUS_PAUSED = "paused"
STATUS_PENDING_VERIFICATION = "pending_verification"
STATUS_RESTORING = "restoring"
STATUS_CLOSED = "closed"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = (STATUS_CLOSED, STATUS_EXPIRED)
PAUSABLE_STATUSES = (STATUS_EXECUTING, STATUS_PENDING_VERIFICATION, STATUS_RESTORING)

ACTIONS = ("start", "close_valve", "verify", "restore_valve", "pause", "resume")

DISCLAIMER = (
    "工单仅模拟演练流程：在冻结快照上维护模拟阀态，不写入真实阀门状态、"
    "不连接真实控制系统，不能作为真实检修隔离（LOTO）的安全依据。"
)


class WorkOrderInputError(ValueError):
    """请求参数问题 → HTTP 400。"""


class WorkOrderConflict(RuntimeError):
    """状态/顺序/前置状态/过期冲突 → HTTP 409。"""

    def __init__(self, message: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.detail = detail or {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 约束指纹


def _constraints_payload(db: Session) -> dict[str, Any]:
    """与隔离约束相关的现场状态：节点类别/必要性、管段连接、阀门开闭/锁定/可操作。"""
    nodes = db.scalars(select(Node)).all()
    segments = db.scalars(select(Segment)).all()
    valves = db.scalars(select(Valve)).all()
    return {
        "nodes": sorted((n.id, n.kind, bool(n.essential)) for n in nodes),
        "segments": sorted(
            (s.id, s.upstream_id, s.downstream_id, s.kind, bool(s.is_bypass)) for s in segments
        ),
        "valves": sorted(
            (v.id, v.segment_id, bool(v.is_open), bool(v.locked), bool(v.operable))
            for v in valves
        ),
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _constraint_diff(db: Session, wo: DrillWorkOrder) -> dict[str, Any]:
    """冻结快照与当前现场的差异（用于过期审计与展示）。"""
    snap = wo.topology_snapshot
    cur = isolation.topology_payload(db)
    cur_valves = {v["id"]: v for v in cur["valves"]}
    valve_changes = []
    for v in snap["valves"]:
        c = cur_valves.get(v["id"])
        if c is None:
            valve_changes.append({"valve_id": v["id"], "change": "removed"})
            continue
        for field in ("is_open", "locked", "operable"):
            if bool(c[field]) != bool(v[field]):
                valve_changes.append(
                    {
                        "valve_id": v["id"],
                        "field": field,
                        "frozen": bool(v[field]),
                        "current": bool(c[field]),
                    }
                )
    cur_nodes = {n["id"]: n for n in cur["nodes"]}
    node_changes = []
    for n in snap["nodes"]:
        c = cur_nodes.get(n["id"])
        if c is None:
            node_changes.append({"node_id": n["id"], "change": "removed"})
            continue
        for field in ("kind", "essential"):
            if c[field] != n[field]:
                node_changes.append(
                    {"node_id": n["id"], "field": field, "frozen": n[field], "current": c[field]}
                )
    cur_segs = {s["id"]: s for s in cur["segments"]}
    segment_changes = []
    for s in snap["segments"]:
        c = cur_segs.get(s["id"])
        if c is None:
            segment_changes.append({"segment_id": s["id"], "change": "removed"})
            continue
        if c["source"] != s["source"] or c["target"] != s["target"]:
            segment_changes.append(
                {
                    "segment_id": s["id"],
                    "change": "rewired",
                    "frozen": [s["source"], s["target"]],
                    "current": [c["source"], c["target"]],
                }
            )
    return {
        "valve_changes": valve_changes,
        "node_changes": node_changes,
        "segment_changes": segment_changes,
    }


# ---------------------------------------------------------------- 快照图与复核


def _snapshot_graph(snapshot: dict[str, Any], simulated: dict[str, bool]) -> nx.Graph:
    """在冻结拓扑快照上套用工单模拟阀态，构建物理连通无向图。"""
    g = nx.Graph()
    for n in snapshot["nodes"]:
        g.add_node(n["id"])
    seg_by_id = {s["id"]: s for s in snapshot["segments"]}
    valved_segments: set[str] = set()
    for v in snapshot["valves"]:
        seg = seg_by_id.get(v["segment_id"])
        if seg is None:
            continue
        valved_segments.add(seg["id"])
        if simulated.get(v["id"], bool(v["is_open"])):
            g.add_edge(seg["source"], seg["target"], edge_id=seg["id"], valve_id=v["id"])
    for s in snapshot["segments"]:
        if s["id"] not in valved_segments:
            g.add_edge(s["source"], s["target"], edge_id=s["id"], valve_id=None)
    return g


def _live_check(wo: DrillWorkOrder) -> dict[str, Any]:
    """NetworkX 复核：冻结快照 + 当前模拟阀态下的隔离与必要供给情况。"""
    snapshot = wo.topology_snapshot
    simulated = wo.simulated_states
    g = _snapshot_graph(snapshot, simulated)
    sources = list(wo.sources)
    target = wo.target_id
    isolated = not isolation._reachable_sources(g, target, sources)
    # 与方案计算口径一致：两条供给路径都真实存在时优先展示经开启旁路的一条
    open_bypass = {
        (s["source"], s["target"])
        for s in snapshot["segments"]
        if s["is_bypass"] and simulated.get(s["valve_id"], True)
    }
    supply_paths = {
        p: isolation._preferred_source_path(g, p, sources, open_bypass) for p in wo.essentials
    }
    residual = None if isolated else isolation._shortest_source_path(g, target, sources)
    closed = sorted(v for v, is_open in simulated.items() if not is_open)
    return {
        "isolated": isolated,
        "all_essentials_supplied": all(supply_paths[p] for p in wo.essentials),
        "supply_paths": supply_paths,
        "residual_path": residual,
        "closed_valves": closed,
    }


# ---------------------------------------------------------------- 审计


def _audit(
    wo: DrillWorkOrder,
    action: str,
    outcome: str,
    idempotency_key: str | None,
    detail: dict[str, Any],
) -> None:
    wo.events.append(
        DrillEvent(
            action=action,
            outcome=outcome,
            idempotency_key=idempotency_key,
            detail=detail,
            ts=_utcnow(),
        )
    )


def _reject(
    db: Session,
    wo: DrillWorkOrder,
    action: str,
    idempotency_key: str | None,
    reason: str,
    detail: dict[str, Any],
) -> None:
    """安全拒绝：落审计并提交（拒绝也必须可追溯），然后抛 409。"""
    payload = {"reason": reason, **detail}
    _audit(wo, action, "rejected", idempotency_key, payload)
    db.commit()
    raise WorkOrderConflict(reason, payload)


def maybe_expire(db: Session, wo: DrillWorkOrder) -> bool:
    """约束指纹不符时把工单置为过期（终态）。返回是否发生了过期迁移。"""
    if wo.status in TERMINAL_STATUSES:
        return False
    current = _fingerprint(_constraints_payload(db))
    if current == wo.fingerprint:
        return False
    diff = _constraint_diff(db, wo)
    wo.status = STATUS_EXPIRED
    wo.resume_to = None
    wo.expired_reason = (
        "执行期间锁定、拓扑或供给约束发生变化，冻结方案不再可信，工单过期，"
        "禁止继续沿用旧方案。"
    )
    _audit(
        wo,
        "system.expire",
        "expired",
        None,
        {"reason": wo.expired_reason, "fingerprint_before": wo.fingerprint,
         "fingerprint_now": current, "diff": diff},
    )
    db.commit()
    return True


# ---------------------------------------------------------------- 创建


def create_work_order(
    db: Session, *, target_id: str, close_valves: list[str], title: str | None = None
) -> DrillWorkOrder:
    nodes, edges = isolation._load(db)
    node_ids = {n.id for n in nodes}
    if target_id not in node_ids:
        raise WorkOrderInputError(f"目标节点不存在: {target_id}")
    sources = sorted(n.id for n in nodes if n.kind == "source")
    essentials = sorted(n.id for n in nodes if n.essential)

    planned = sorted(dict.fromkeys(close_valves))
    valves_by_id = {e["valve_id"]: e for e in edges.values() if e["valve_id"]}
    unknown = [v for v in planned if v not in valves_by_id]
    if unknown:
        raise WorkOrderInputError(f"未知阀门: {', '.join(unknown)}")
    locked = [v for v in planned if valves_by_id[v]["locked"]]
    if locked:
        raise WorkOrderInputError(f"所选阀门已被锁定（禁止关闭）: {', '.join(locked)}")
    inoperable = [v for v in planned if not valves_by_id[v]["operable"]]
    if inoperable:
        raise WorkOrderInputError(f"所选阀门工艺上不可操作: {', '.join(inoperable)}")
    not_open = [v for v in planned if not valves_by_id[v]["is_open"]]
    if not_open:
        raise WorkOrderInputError(f"所选阀门当前不是打开状态: {', '.join(not_open)}")

    # 用 NetworkX 复核所选集合在当前约束下确为有效隔离方案
    closed = set(planned)
    g = isolation.build_graph(edges, closed)
    if isolation._reachable_sources(g, target_id, sources):
        raise WorkOrderInputError("所选阀门集合不能切断目标与来源的连通，不是有效隔离方案")
    lost = [p for p in essentials if not isolation._reachable_sources(g, p, sources)]
    if lost:
        raise WorkOrderInputError(f"所选方案会断供必要供给点: {', '.join(lost)}")

    open_bypass = {
        (e["u"], e["v"])
        for e in edges.values()
        if e["is_bypass"] and e["valve_id"] not in closed and e["is_open"]
    }
    planned_supply = {
        p: isolation._preferred_source_path(g, p, sources, open_bypass) for p in essentials
    }

    snapshot = isolation.topology_payload(db)
    fingerprint = _fingerprint(_constraints_payload(db))
    simulated = {v["id"]: bool(v["is_open"]) for v in snapshot["valves"]}
    lock_snapshot = {v["id"]: bool(v["locked"]) for v in snapshot["valves"]}

    wo = DrillWorkOrder(
        id="WO-" + uuid.uuid4().hex[:8].upper(),
        title=title or f"隔离演练：{target_id}",
        target_id=target_id,
        status=STATUS_DRAFT,
        sources=sources,
        essentials=essentials,
        planned_close_valves=planned,
        planned_supply_paths=planned_supply,
        lock_snapshot=lock_snapshot,
        topology_snapshot=snapshot,
        fingerprint=fingerprint,
        simulated_states=simulated,
    )
    seq = 0
    for vid in planned:
        seq += 1
        wo.steps.append(
            DrillStep(
                seq=seq,
                kind="close_valve",
                valve_id=vid,
                expected_pre={"status": STATUS_EXECUTING, "valve_is_open": True},
            )
        )
    seq += 1
    wo.steps.append(
        DrillStep(
            seq=seq,
            kind="verify",
            valve_id=None,
            expected_pre={"status": STATUS_PENDING_VERIFICATION},
        )
    )
    for vid in reversed(planned):
        seq += 1
        wo.steps.append(
            DrillStep(
                seq=seq,
                kind="restore_valve",
                valve_id=vid,
                expected_pre={"status": STATUS_RESTORING, "valve_is_open": False},
            )
        )
    _audit(
        wo,
        "create",
        "applied",
        None,
        {
            "target_id": target_id,
            "planned_close_valves": planned,
            "essentials": essentials,
            "fingerprint": fingerprint,
            "disclaimer": DISCLAIMER,
        },
    )
    db.add(wo)
    db.commit()
    db.refresh(wo)
    return wo


# ---------------------------------------------------------------- 事件处理


def _next_step(wo: DrillWorkOrder) -> DrillStep | None:
    for s in sorted(wo.steps, key=lambda s: s.seq):
        if s.status == "pending":
            return s
    return None


def _find_applied_event(wo: DrillWorkOrder, key: str) -> DrillEvent | None:
    for e in wo.events:
        if e.idempotency_key == key and e.outcome == "applied":
            return e
    return None


def _step_brief(step: DrillStep | None) -> dict[str, Any] | None:
    if step is None:
        return None
    return {"seq": step.seq, "kind": step.kind, "valve_id": step.valve_id}


def _check_expected(
    db: Session,
    wo: DrillWorkOrder,
    action: str,
    key: str,
    expected: dict[str, Any],
    step: DrillStep | None = None,
) -> None:
    """复核客户端声明的前置状态；不符则安全拒绝（不跳过任何步骤）。"""
    if "status" not in expected:
        _reject(db, wo, action, key, "缺少前置状态 expected.status", {"expected": expected})
    if expected["status"] != wo.status:
        _reject(
            db, wo, action, key,
            f"前置状态不符：客户端期望工单状态 {expected['status']}，实际 {wo.status}",
            {"expected": expected, "actual_status": wo.status},
        )
    if step is not None:
        if "step_seq" not in expected:
            _reject(db, wo, action, key, "缺少前置状态 expected.step_seq",
                    {"expected": expected, "actual_next_step": _step_brief(step)})
        if expected["step_seq"] != step.seq:
            _reject(
                db, wo, action, key,
                f"前置状态不符：客户端期望下一步骤 seq={expected['step_seq']}，实际 seq={step.seq}",
                {"expected": expected, "actual_next_step": _step_brief(step)},
            )
        if step.valve_id and "valve_is_open" in expected:
            actual_open = bool(wo.simulated_states.get(step.valve_id, True))
            if bool(expected["valve_is_open"]) != actual_open:
                _reject(
                    db, wo, action, key,
                    f"前置状态不符：阀门 {step.valve_id} 模拟阀态与客户端期望不一致",
                    {
                        "expected": expected,
                        "actual_valve_is_open": actual_open,
                        "valve_id": step.valve_id,
                    },
                )


def _complete_step(wo: DrillWorkOrder, step: DrillStep, key: str, evidence: dict[str, Any]) -> None:
    step.status = "done"
    step.idempotency_key = key
    step.evidence = evidence
    step.completed_at = _utcnow()


def _apply_close(wo: DrillWorkOrder, step: DrillStep, key: str) -> dict[str, Any]:
    vid = step.valve_id
    simulated = dict(wo.simulated_states)
    simulated[vid] = False
    wo.simulated_states = simulated
    check = _live_check(wo)
    evidence = {
        "valve_id": vid,
        "simulated_is_open_after": False,
        "isolated_after": check["isolated"],
        "all_essentials_supplied_after": check["all_essentials_supplied"],
        "supply_paths_after": check["supply_paths"],
        "residual_path_after": check["residual_path"],
        "note": "模拟关阀：仅更新工单快照内的模拟阀态，未触碰真实阀门。",
    }
    _complete_step(wo, step, key, evidence)
    if not any(s.status == "pending" and s.kind == "close_valve" for s in wo.steps):
        wo.status = STATUS_PENDING_VERIFICATION
    return evidence


def _apply_verify(db: Session, wo: DrillWorkOrder, step: DrillStep, key: str) -> dict[str, Any]:
    check = _live_check(wo)
    planned_closed = all(not wo.simulated_states.get(v, True) for v in wo.planned_close_valves)
    verified = bool(check["isolated"] and check["all_essentials_supplied"] and planned_closed)
    evidence = {
        "verified": verified,
        "isolated": check["isolated"],
        "all_essentials_supplied": check["all_essentials_supplied"],
        "supply_paths": check["supply_paths"],
        "closed_valves": check["closed_valves"],
        "planned_valves_all_closed": planned_closed,
        "checked_against": "frozen_topology_snapshot",
        "recheck_engine": "networkx",
        "note": "核验复核在工单冻结快照 + 模拟阀态上进行，不连接真实控制系统。",
    }
    if not verified:
        _reject(
            db, wo, "verify", key,
            "核验未通过：冻结快照上的模拟阀态未形成有效隔离或必要供给断供",
            {"evidence": evidence},
        )
    _complete_step(wo, step, key, evidence)
    wo.status = STATUS_RESTORING
    return evidence


def _apply_restore(wo: DrillWorkOrder, step: DrillStep, key: str) -> dict[str, Any]:
    vid = step.valve_id
    frozen_open = next(
        (bool(v["is_open"]) for v in wo.topology_snapshot["valves"] if v["id"] == vid), True
    )
    simulated = dict(wo.simulated_states)
    simulated[vid] = frozen_open
    wo.simulated_states = simulated
    check = _live_check(wo)
    evidence = {
        "valve_id": vid,
        "restored_to_frozen_state": frozen_open,
        "simulated_is_open_after": simulated[vid],
        "closed_valves_after": check["closed_valves"],
        "note": "模拟复位：依据工单冻结的阀态恢复，未触碰真实阀门。",
    }
    _complete_step(wo, step, key, evidence)
    if not any(s.status == "pending" and s.kind == "restore_valve" for s in wo.steps):
        wo.status = STATUS_CLOSED
    return evidence


def post_event(
    db: Session,
    wo: DrillWorkOrder,
    *,
    action: str,
    valve_id: str | None,
    idempotency_key: str,
    expected: dict[str, Any],
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise WorkOrderInputError(f"未知工单动作: {action}（允许: {', '.join(ACTIONS)}）")
    if not idempotency_key:
        raise WorkOrderInputError("缺少幂等标识 idempotency_key")

    # 1) 约束指纹失效 → 过期（终态），阻止继续沿用旧方案
    if maybe_expire(db, wo):
        raise WorkOrderConflict(
            wo.expired_reason or "工单已过期",
            {"status": wo.status, "expired_reason": wo.expired_reason},
        )

    # 2) 幂等重放：同一幂等标识直接返回首次记录的结果，不重复应用
    prev = _find_applied_event(wo, idempotency_key)
    if prev is not None:
        _audit(wo, action, "duplicate", idempotency_key, {"replay_of_event_id": prev.id})
        db.commit()
        return {
            "outcome": "duplicate",
            "work_order_id": wo.id,
            "status": wo.status,
            "recorded": prev.detail,
        }

    # 3) 终态拒绝
    if wo.status in TERMINAL_STATUSES:
        _reject(
            db, wo, action, idempotency_key,
            f"工单已处于终态 {wo.status}，禁止继续操作",
            {"status": wo.status, "expired_reason": wo.expired_reason},
        )

    # 4) 前置状态复核（客户端声明 vs 服务端实际）
    _check_expected(db, wo, action, idempotency_key, expected)

    # 5) 分发
    evidence: dict[str, Any] | None = None
    step: DrillStep | None = None
    if action == "start":
        if wo.status != STATUS_DRAFT:
            _reject(db, wo, action, idempotency_key,
                    f"仅草稿状态可开始执行，当前 {wo.status}", {"status": wo.status})
        nxt = _next_step(wo)
        wo.status = STATUS_EXECUTING if (nxt and nxt.kind == "close_valve") else STATUS_PENDING_VERIFICATION
    elif action == "pause":
        if wo.status not in PAUSABLE_STATUSES:
            _reject(db, wo, action, idempotency_key,
                    f"当前状态 {wo.status} 不可暂停", {"status": wo.status})
        wo.resume_to = wo.status
        wo.status = STATUS_PAUSED
    elif action == "resume":
        if wo.status != STATUS_PAUSED:
            _reject(db, wo, action, idempotency_key,
                    f"仅暂停状态可恢复，当前 {wo.status}", {"status": wo.status})
        wo.status = wo.resume_to or STATUS_EXECUTING
        wo.resume_to = None
    else:
        # 计划步骤：close_valve / verify / restore_valve —— 必须严格按序，
        # 乱序事件安全拒绝且不跳过任何步骤
        required_status = {
            "close_valve": STATUS_EXECUTING,
            "verify": STATUS_PENDING_VERIFICATION,
            "restore_valve": STATUS_RESTORING,
        }[action]
        if wo.status != required_status:
            _reject(db, wo, action, idempotency_key,
                    f"动作 {action} 要求工单状态 {required_status}，当前 {wo.status}",
                    {"status": wo.status})
        step = _next_step(wo)
        if step is None or step.kind != action or (action != "verify" and step.valve_id != valve_id):
            _reject(
                db, wo, action, idempotency_key,
                "乱序事件：与下一个待执行步骤不符，已安全拒绝（未跳过任何步骤）",
                {"actual_next_step": _step_brief(step), "requested_valve_id": valve_id},
            )
        # 步骤级前置状态（step_seq / 阀态）
        _check_expected(db, wo, action, idempotency_key, expected, step)
        if action == "close_valve":
            evidence = _apply_close(wo, step, idempotency_key)
        elif action == "verify":
            evidence = _apply_verify(db, wo, step, idempotency_key)
        else:
            evidence = _apply_restore(wo, step, idempotency_key)

    detail = {
        "status_after": wo.status,
        "step": _step_brief(step),
        "evidence": evidence,
    }
    _audit(wo, action, "applied", idempotency_key, detail)
    db.commit()
    return {
        "outcome": "applied",
        "work_order_id": wo.id,
        "status": wo.status,
        "action": action,
        "step": _step_brief(step),
        "evidence": evidence,
        "live_check": _live_check(wo),
    }


# ---------------------------------------------------------------- 查询载荷


def _event_view(e: DrillEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "ts": e.ts.isoformat() if e.ts else None,
        "action": e.action,
        "outcome": e.outcome,
        "idempotency_key": e.idempotency_key,
        "detail": e.detail,
    }


def _step_view(s: DrillStep) -> dict[str, Any]:
    return {
        "seq": s.seq,
        "kind": s.kind,
        "valve_id": s.valve_id,
        "status": s.status,
        "expected_pre": s.expected_pre,
        "idempotency_key": s.idempotency_key,
        "evidence": s.evidence,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
    }


def detail_payload(db: Session, wo: DrillWorkOrder) -> dict[str, Any]:
    maybe_expire(db, wo)
    steps = sorted(wo.steps, key=lambda s: s.seq)
    nxt = _next_step(wo)
    applied = [e for e in wo.events if e.outcome == "applied"]
    return {
        "id": wo.id,
        "title": wo.title,
        "target_id": wo.target_id,
        "status": wo.status,
        "resume_to": wo.resume_to,
        "sources": wo.sources,
        "essentials": wo.essentials,
        "planned_close_valves": wo.planned_close_valves,
        "planned_supply_paths": wo.planned_supply_paths,
        "lock_snapshot": wo.lock_snapshot,
        "simulated_states": wo.simulated_states,
        "topology_snapshot": wo.topology_snapshot,
        "fingerprint": wo.fingerprint,
        "expired_reason": wo.expired_reason,
        "steps": [_step_view(s) for s in steps],
        "next_step": _step_brief(nxt),
        "live_check": _live_check(wo),
        "audit": [_event_view(e) for e in sorted(wo.events, key=lambda e: e.id)],
        "applied_event_count": len(applied),
        "disclaimer": DISCLAIMER,
        "created_at": wo.created_at.isoformat() if wo.created_at else None,
        "updated_at": wo.updated_at.isoformat() if wo.updated_at else None,
    }


def list_payload(db: Session) -> list[dict[str, Any]]:
    orders = list(db.scalars(select(DrillWorkOrder)).all())
    for wo in orders:
        maybe_expire(db, wo)  # 过期迁移自身会提交
    out = []
    for wo in orders:
        steps = list(wo.steps)
        done = sum(1 for s in steps if s.status == "done")
        out.append(
            {
                "id": wo.id,
                "title": wo.title,
                "target_id": wo.target_id,
                "status": wo.status,
                "planned_close_valves": wo.planned_close_valves,
                "steps_total": len(steps),
                "steps_done": done,
                "expired_reason": wo.expired_reason,
                "created_at": wo.created_at.isoformat() if wo.created_at else None,
            }
        )
    out.sort(key=lambda w: w["created_at"] or "", reverse=True)
    return out
