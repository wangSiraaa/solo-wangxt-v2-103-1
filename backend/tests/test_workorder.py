"""隔离演练工单验收测试。

覆盖验收标准：
1) 可行方案按步骤执行，并在核验前显示目标隔离及必要供给路径；
2) 执行中锁定计划内阀门后工单变过期，且不产生部分假完成；
3) 重复提交关阀 / 先提交核验等乱序事件被安全处理并保留审计；
4) 暂停、刷新、恢复再复位后，原始拓扑状态、历史路径和工单证据均能正确重放。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Valve, WorkOrder
from app.seed import seed_database


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


def _create_order(client: TestClient, **overrides) -> dict:
    body = {"target_id": "T", "locks": None, "solution_rank": 0, "title": None}
    body.update(overrides)
    r = client.post("/api/workorders", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _event(client: TestClient, oid: str, seq: int, type_: str, expected: str,
           valve_id: str | None = None, key: str | None = None):
    """提交事件；key 缺省时按序号生成确定性的幂等标识。"""
    return client.post(
        f"/api/workorders/{oid}/events",
        json={
            "type": type_,
            "idempotency_key": key or f"k-{seq}",
            "expected_status": expected,
            "valve_id": valve_id,
            "note": None,
        },
    )


def _ok(client: TestClient, oid: str, seq: int, type_: str, expected: str,
        valve_id: str | None = None, key: str | None = None) -> dict:
    r = _event(client, oid, seq, type_, expected, valve_id, key)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- 验收 1：按步骤执行，核验前显示目标隔离与必要供给路径 ----------------

def test_acceptance_1_stepwise_execution_shows_isolation_and_supply_before_verify():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create_order(client)
        oid = wo["id"]
        # 创建即冻结：草稿、方案、快照、指纹、免责声明
        assert wo["status"] == "draft"
        assert wo["plan"]["close_valves"] == ["V_TIN", "V_TOUT"]
        assert "不连接真实控制系统" in wo["disclaimer"]
        assert wo["snapshot"]["essentials"] == ["P1", "P2"]
        assert len(wo["fingerprint"]) == 64
        # 计划步骤：关阀×2 -> 核验 -> 复位×2
        kinds = [(s["kind"], s["valve_id"]) for s in wo["steps"]]
        assert kinds == [
            ("close_valve", "V_TIN"),
            ("close_valve", "V_TOUT"),
            ("verify", None),
            ("restore_valve", "V_TIN"),
            ("restore_valve", "V_TOUT"),
        ]
        assert all(s["status"] == "pending" for s in wo["steps"])

        wo = _ok(client, oid, 1, "start", "draft")
        assert wo["status"] == "executing"

        # 第一步关阀后：尚未完全隔离（介质可经旁路/环网绕回），但路径可见
        wo = _ok(client, oid, 2, "close_valve", "executing", "V_TIN")
        assert wo["status"] == "executing"
        assert wo["simulation"]["simulated_closed_valves"] == ["V_TIN"]
        assert wo["simulation"]["isolation_ok"] is False
        assert wo["simulation"]["supply_paths"]["P2"]  # 核验前即可见供给路径

        # 第二步关阀后：核验前即显示目标已隔离 + 必要供给点路径
        wo = _ok(client, oid, 3, "close_valve", "executing", "V_TOUT")
        assert wo["status"] == "pending_verification"
        sim = wo["simulation"]
        assert sim["isolation_ok"] is True
        assert sim["essentials_all_supplied"] is True
        assert sim["supply_paths"]["P1"] == ["SRC", "N1", "P1"]
        assert "BP" in sim["supply_paths"]["P2"]  # 旁路绕回供 P2
        assert sim["valve_states"]["V_TIN"] == "sim_closed"
        assert sim["valve_states"]["V_TOUT"] == "sim_closed"

        # 核验：NetworkX 复核通过，证据落库
        wo = _ok(client, oid, 4, "verify", "pending_verification")
        assert wo["status"] == "restoring"
        verify_step = next(s for s in wo["steps"] if s["kind"] == "verify")
        assert verify_step["status"] == "done"
        ev = verify_step["evidence"]
        assert ev["engine"] == "networkx"
        assert ev["isolation_ok"] is True
        assert ev["essentials_all_supplied"] is True
        assert ev["supply_paths"]["P2"]
        assert ev["fingerprint"] == wo["fingerprint"]


# ---------------- 验收 2：执行中锁定计划内阀门 => 工单过期，无部分假完成 ----------------

def test_acceptance_2_locking_planned_valve_stales_order_without_fake_completion():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create_order(client)
        oid = wo["id"]
        _ok(client, oid, 1, "start", "draft")
        _ok(client, oid, 2, "close_valve", "executing", "V_TIN")

        # 执行中：外部把计划内阀门 V_TOUT 锁定（冻结前提被改变）
        r = client.post("/api/valves/V_TOUT/lock", json={"locked": True})
        assert r.status_code == 200

        # 继续提交下一步关阀 => 409，工单落库为过期
        r = _event(client, oid, 3, "close_valve", "executing", "V_TOUT")
        assert r.status_code == 409
        assert "过期" in r.json()["detail"]

        wo = client.get(f"/api/workorders/{oid}").json()
        assert wo["status"] == "stale"
        # 不产生部分假完成：只有第一步留痕，核验/复位均未发生
        done = [s for s in wo["steps"] if s["status"] == "done"]
        assert len(done) == 1 and done[0]["valve_id"] == "V_TIN"
        verify_step = next(s for s in wo["steps"] if s["kind"] == "verify")
        assert verify_step["status"] == "pending" and verify_step["evidence"] is None
        # 审计完整：stale_marked + rejected 都留痕
        types = [(e["type"], e["result"]) for e in wo["events"]]
        assert ("stale_marked", "accepted") in types
        assert ("close_valve", "rejected") in types

        # 过期后任何事件（核验/暂停/恢复/复位）都被阻止
        for type_, expected, vid in [
            ("verify", "pending_verification", None),
            ("pause", "executing", None),
            ("resume", "paused", None),
            ("restore_valve", "restoring", "V_TIN"),
        ]:
            r = _event(client, oid, 90, type_, expected, vid)
            assert r.status_code == 409, (type_, r.text)
        wo = client.get(f"/api/workorders/{oid}").json()
        assert wo["status"] == "stale"
        assert len([s for s in wo["steps"] if s["status"] == "done"]) == 1


# ---------------- 验收 3：重复提交与乱序事件安全处理并保留审计 ----------------

def test_acceptance_3_duplicate_and_out_of_order_events_are_safe_and_audited():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create_order(client)
        oid = wo["id"]

        # 乱序 1：草稿状态先提交核验（前置状态不符）
        r = _event(client, oid, 1, "verify", "pending_verification")
        assert r.status_code == 409

        _ok(client, oid, 2, "start", "draft")

        # 乱序 2：执行中直接提交核验（前置状态匹配但流程不允许）
        r = _event(client, oid, 3, "verify", "executing")
        assert r.status_code == 409
        assert "核验" in r.json()["detail"]

        # 乱序 3：跳步关阀（应先关 V_TIN）
        r = _event(client, oid, 4, "close_valve", "executing", "V_TOUT")
        assert r.status_code == 409
        assert "跳步" in r.json()["detail"] or "乱序" in r.json()["detail"]

        # 正常关阀，随后用同一幂等标识重复提交
        wo = _ok(client, oid, 5, "close_valve", "executing", "V_TIN", key="dup-key")
        assert wo["last_event"]["duplicate"] is False
        wo = _ok(client, oid, 6, "close_valve", "executing", "V_TIN", key="dup-key")
        assert wo["last_event"]["duplicate"] is True
        # 重复提交没有重复执行：仍只有 1 步完成，模拟关闭集合不变
        done = [s for s in wo["steps"] if s["status"] == "done"]
        assert len(done) == 1 and done[0]["valve_id"] == "V_TIN"
        assert wo["simulation"]["simulated_closed_valves"] == ["V_TIN"]
        assert wo["status"] == "executing"

        # 审计：拒绝与重复全部留痕
        events = wo["events"]
        rejected = [e for e in events if e["result"] == "rejected"]
        duplicates = [e for e in events if e["result"] == "duplicate"]
        assert len(rejected) == 3
        assert len(duplicates) == 1
        assert duplicates[0]["payload"]["replay_of"] == wo["last_event"]["seq"]
        assert all(e["idempotency_key"] for e in events if e["type"] != "create")

        # 乱序事件没有跳过任何步骤：流程仍可正常走完
        wo = _ok(client, oid, 7, "close_valve", "executing", "V_TOUT")
        assert wo["status"] == "pending_verification"
        wo = _ok(client, oid, 8, "verify", "pending_verification")
        assert wo["status"] == "restoring"


# ---------------- 验收 4：暂停-刷新-恢复-复位后完整重放 ----------------

def test_acceptance_4_pause_refresh_resume_restore_replays_everything():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create_order(client)
        oid = wo["id"]
        _ok(client, oid, 1, "start", "draft")
        _ok(client, oid, 2, "close_valve", "executing", "V_TIN")
        _ok(client, oid, 3, "close_valve", "executing", "V_TOUT")

        wo = _ok(client, oid, 4, "pause", "pending_verification")
        assert wo["status"] == "paused"
        assert wo["resume_status"] == "pending_verification"

        # 暂停中不允许继续步骤
        r = _event(client, oid, 5, "verify", "pending_verification")
        assert r.status_code == 409

    # “刷新”：丢掉内存会话，用全新连接从持久化存储重读
    engine.dispose()
    db = SessionLocal()
    persisted = db.get(WorkOrder, oid)
    assert persisted is not None and persisted.status == "paused"
    assert len(persisted.events) >= 5  # create/start/close×2/pause + 暂停中的拒绝
    assert sum(1 for s in persisted.steps if s.status == "done") == 2
    db.close()

    with TestClient(app) as client:
        # 刷新后：中断前的模拟状态与路径仍可重放（依据冻结快照 + 已完成步骤）
        wo = client.get(f"/api/workorders/{oid}").json()
        assert wo["status"] == "paused"
        assert wo["simulation"]["isolation_ok"] is True
        assert wo["simulation"]["supply_paths"]["P2"]
        assert wo["simulation"]["simulated_closed_valves"] == ["V_TIN", "V_TOUT"]

        # 恢复 -> 核验 -> 复位 -> 关闭
        wo = _ok(client, oid, 6, "resume", "paused")
        assert wo["status"] == "pending_verification"
        wo = _ok(client, oid, 7, "verify", "pending_verification")
        assert wo["status"] == "restoring"
        wo = _ok(client, oid, 8, "restore_valve", "restoring", "V_TIN")
        assert wo["status"] == "restoring"
        assert wo["simulation"]["simulated_closed_valves"] == ["V_TOUT"]
        wo = _ok(client, oid, 9, "restore_valve", "restoring", "V_TOUT")
        assert wo["status"] == "closed"

        # 重放 1：原始拓扑状态恢复（模拟视角全部打开，目标恢复连通）
        sim = wo["simulation"]
        assert sim["simulated_closed_valves"] == []
        assert set(sim["valve_states"].values()) == {"open"}
        assert sim["isolation_ok"] is False
        # 重放 2：历史路径仍在（复位后来源重新到达各必要供给点）
        assert sim["supply_paths"]["P1"] == ["SRC", "N1", "P1"]
        assert sim["supply_paths"]["P2"]
        # 重放 3：工单证据完整保留（步骤证据 + 核验证据 + 审计链）
        assert all(s["status"] == "done" for s in wo["steps"])
        verify_step = next(s for s in wo["steps"] if s["kind"] == "verify")
        assert verify_step["evidence"]["isolation_ok"] is True
        assert verify_step["evidence"]["supply_paths"]["P2"]
        close1 = wo["steps"][0]
        assert close1["evidence"]["simulated_closed"] == ["V_TIN"]
        event_types = [e["type"] for e in wo["events"]]
        assert event_types == [
            "create", "start", "close_valve", "close_valve", "pause",
            "verify",  # 暂停中被拒绝的那次核验也留痕
            "resume", "verify", "restore_valve", "restore_valve",
        ]
        assert [e["result"] for e in wo["events"]].count("rejected") == 1

        # 模拟从未触碰真实阀门表：原始拓扑状态未被演练改变
        topo = client.get("/api/topology").json()
        assert all(v["is_open"] and not v["locked"] for v in topo["valves"])
        db = SessionLocal()
        assert all(v.is_open and not v.locked for v in db.query(Valve).all())
        db.close()


def test_work_order_rejected_when_infeasible_and_unknown_valve():
    _reset_seed()
    with TestClient(app) as client:
        # 样例 3 场景（锁定出口阀）无可行方案 => 不能生成工单
        r = client.post("/api/workorders",
                        json={"target_id": "T", "locks": {"V_TOUT": True}})
        assert r.status_code == 400
        # 未知阀门 => 400
        r = client.post("/api/workorders",
                        json={"target_id": "T", "locks": {"NOPE": True}})
        assert r.status_code == 400
        # 未知目标 => 400
        r = client.post("/api/workorders", json={"target_id": "GHOST"})
        assert r.status_code == 400
        # 不存在的工单 => 404
        assert client.get("/api/workorders/WO-NOPE").status_code == 404
        # 列表接口可用且带免责声明
        listing = client.get("/api/workorders").json()
        assert listing["orders"] == []
        assert "不连接真实控制系统" in listing["disclaimer"]
