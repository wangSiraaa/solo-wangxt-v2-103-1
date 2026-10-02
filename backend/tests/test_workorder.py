"""隔离演练工单验收测试。

覆盖验收标准：
1) 可行方案按步骤执行，并在核验前显示目标隔离及必要供给路径；
2) 执行中锁定计划内阀门 → 工单过期，且不产生部分假完成；
3) 重复提交关阀 / 先提交核验等乱序事件被安全处理并保留审计；
4) 暂停、刷新、恢复再复位后，原始拓扑状态、历史路径与工单证据正确重放。

另验证：工单全程不写入真实阀门表（只模拟流程）。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed_database


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


def _create(client: TestClient, valves=("V_TIN", "V_TOUT"), title="演练工单") -> dict:
    r = client.post(
        "/api/work-orders",
        json={"target_id": "T", "close_valves": list(valves), "title": title},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _event(client: TestClient, wo_id: str, action: str, key: str,
           valve_id: str | None = None, expected: dict | None = None):
    return client.post(
        f"/api/work-orders/{wo_id}/events",
        json={
            "action": action,
            "valve_id": valve_id,
            "idempotency_key": key,
            "expected": expected or {},
        },
    )


def _start(client: TestClient, wo: dict) -> None:
    r = _event(client, wo["id"], "start", key=f"{wo['id']}:start",
               expected={"status": "draft"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executing"


def _close(client: TestClient, wo_id: str, seq: int, valve: str, key: str | None = None):
    return _event(
        client, wo_id, "close_valve", key=key or f"{wo_id}:step:{seq}",
        valve_id=valve, expected={"status": "executing", "step_seq": seq, "valve_is_open": True},
    )


# ---------------- 验收 1：按步骤执行，核验前可见隔离与供给路径 ----------------

def test_acceptance_1_stepwise_execution_and_paths_before_verify():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create(client)
        assert wo["status"] == "draft"
        # 冻结内容完整：目标 / 必要供给点 / 锁定状态 / 拓扑快照 / 所选方案
        assert wo["target_id"] == "T"
        assert wo["essentials"] == ["P1", "P2"]
        assert wo["planned_close_valves"] == ["V_TIN", "V_TOUT"]
        assert wo["lock_snapshot"] == {v: False for v in wo["lock_snapshot"]}
        assert len(wo["topology_snapshot"]["valves"]) == 10
        assert "不连接真实控制系统" in wo["disclaimer"]
        # 计划步骤：关 V_TIN → 关 V_TOUT → 核验 → 复位 V_TOUT → 复位 V_TIN
        kinds = [(s["seq"], s["kind"], s["valve_id"]) for s in wo["steps"]]
        assert kinds == [
            (1, "close_valve", "V_TIN"),
            (2, "close_valve", "V_TOUT"),
            (3, "verify", None),
            (4, "restore_valve", "V_TOUT"),
            (5, "restore_valve", "V_TIN"),
        ]

        _start(client, wo)

        # 第一步关 V_TIN：尚未隔离（旁路/环网绕回），残余路径可见
        r = _close(client, wo["id"], 1, "V_TIN")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["live_check"]["isolated"] is False
        assert body["live_check"]["residual_path"] is not None

        # 第二步关 V_TOUT：进入待核验，核验前即可见目标隔离 + 必要供给路径
        r = _close(client, wo["id"], 2, "V_TOUT")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_verification"

        detail = client.get(f"/api/work-orders/{wo['id']}").json()
        assert detail["status"] == "pending_verification"
        check = detail["live_check"]
        assert check["isolated"] is True                      # 核验前：目标已隔离
        assert check["all_essentials_supplied"] is True
        assert check["supply_paths"]["P1"] is not None        # 核验前：必要供给路径可见
        assert check["supply_paths"]["P2"] is not None
        assert "BP" in check["supply_paths"]["P2"]            # P2 经旁路绕回
        assert detail["next_step"]["kind"] == "verify"

        # 核验：NetworkX 复核证据
        r = _event(client, wo["id"], "verify", key=f"{wo['id']}:step:3",
                   expected={"status": "pending_verification", "step_seq": 3})
        assert r.status_code == 200, r.text
        ev = r.json()["evidence"]
        assert ev["verified"] is True
        assert ev["isolated"] is True
        assert ev["supply_paths"]["P2"] is not None
        assert r.json()["status"] == "restoring"

        # 复位（逆序）：V_TOUT → V_TIN，之后工单关闭
        r = _event(client, wo["id"], "restore_valve", key=f"{wo['id']}:step:4",
                   valve_id="V_TOUT",
                   expected={"status": "restoring", "step_seq": 4, "valve_is_open": False})
        assert r.status_code == 200, r.text
        r = _event(client, wo["id"], "restore_valve", key=f"{wo['id']}:step:5",
                   valve_id="V_TIN",
                   expected={"status": "restoring", "step_seq": 5, "valve_is_open": False})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "closed"

        # 工单只模拟流程：真实阀门表全程未被写入
        topo = client.get("/api/topology").json()
        assert all(v["is_open"] and not v["locked"] for v in topo["valves"])


# ---------------- 验收 2：执行中锁定计划内阀门 → 过期，无部分假完成 ----------------

def test_acceptance_2_locking_planned_valve_expires_order():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create(client)
        _start(client, wo)
        r = _close(client, wo["id"], 1, "V_TIN")
        assert r.status_code == 200, r.text

        # 执行中锁定计划内阀门 V_TOUT（真实约束变化）
        client.post("/api/valves/V_TOUT/lock", json={"locked": True})

        # 继续执行被拒：工单过期
        r = _close(client, wo["id"], 2, "V_TOUT")
        assert r.status_code == 409, r.text
        detail = client.get(f"/api/work-orders/{wo['id']}").json()
        assert detail["status"] == "expired"
        assert detail["expired_reason"]
        # 过期差异可追溯到 V_TOUT 的锁定变化
        expired_events = [e for e in detail["audit"] if e["outcome"] == "expired"]
        assert expired_events, detail["audit"]
        changed = expired_events[0]["detail"]["diff"]["valve_changes"]
        assert any(c["valve_id"] == "V_TOUT" and c["field"] == "locked" for c in changed)

        # 不产生部分假完成：核验/复位/任何后续动作均被阻止
        assert detail["steps"][0]["status"] == "done"        # 历史步骤保留为证据
        assert all(s["status"] == "pending" for s in detail["steps"][1:])
        r = _event(client, wo["id"], "verify", key="k-verify-after-expire",
                   expected={"status": "expired"})
        assert r.status_code == 409
        r = _event(client, wo["id"], "pause", key="k-pause-after-expire",
                   expected={"status": "expired"})
        assert r.status_code == 409
        detail = client.get(f"/api/work-orders/{wo['id']}").json()
        assert detail["status"] == "expired"                  # 不会推进到 closed
        # 列表视图同样反映过期
        listing = client.get("/api/work-orders").json()
        assert listing[0]["status"] == "expired"


# ---------------- 验收 3：重复与乱序事件安全处理并保留审计 ----------------

def test_acceptance_3_duplicate_and_out_of_order_events_are_safe():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create(client)
        wid = wo["id"]
        _start(client, wo)

        # 正常关第 1 步
        r = _close(client, wid, 1, "V_TIN", key="dup-key-1")
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "applied"

        # 同一幂等标识重复提交 → 幂等重放，不重复应用
        r = _close(client, wid, 1, "V_TIN", key="dup-key-1")
        assert r.status_code == 200, r.text
        assert r.json()["outcome"] == "duplicate"
        detail = client.get(f"/api/work-orders/{wid}").json()
        assert sum(1 for s in detail["steps"] if s["status"] == "done") == 1
        assert any(e["outcome"] == "duplicate" for e in detail["audit"])

        # 同一阀门换个幂等标识再提交 → 乱序拒绝，状态不变
        r = _close(client, wid, 1, "V_TIN", key="dup-key-2")
        assert r.status_code == 409, r.text
        detail = client.get(f"/api/work-orders/{wid}").json()
        assert sum(1 for s in detail["steps"] if s["status"] == "done") == 1
        assert detail["next_step"]["seq"] == 2

        # 乱序：先提交核验 → 安全拒绝，不跳过任何步骤
        r = _event(client, wid, "verify", key="early-verify",
                   expected={"status": "executing", "step_seq": 3})
        assert r.status_code == 409, r.text
        detail = client.get(f"/api/work-orders/{wid}").json()
        assert detail["status"] == "executing"
        assert detail["next_step"]["seq"] == 2
        rejected = [e for e in detail["audit"] if e["outcome"] == "rejected"]
        assert any(e["action"] == "verify" for e in rejected)

        # 前置状态造假（声明的阀态/步骤号与实际不符）→ 拒绝
        r = _event(client, wid, "close_valve", key="bad-pre", valve_id="V_TOUT",
                   expected={"status": "executing", "step_seq": 99, "valve_is_open": True})
        assert r.status_code == 409, r.text

        # 乱序拒绝后流程仍可正确继续：关 V_TOUT → 核验 → 复位 → 关闭
        r = _close(client, wid, 2, "V_TOUT")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_verification"
        r = _event(client, wid, "verify", key=f"{wid}:step:3",
                   expected={"status": "pending_verification", "step_seq": 3})
        assert r.status_code == 200, r.text
        for seq, valve in ((4, "V_TOUT"), (5, "V_TIN")):
            r = _event(client, wid, "restore_valve", key=f"{wid}:step:{seq}", valve_id=valve,
                       expected={"status": "restoring", "step_seq": seq, "valve_is_open": False})
            assert r.status_code == 200, r.text
        assert r.json()["status"] == "closed"

        # 审计完整保留：重复、拒绝、应用全部可追溯
        detail = client.get(f"/api/work-orders/{wid}").json()
        outcomes = [e["outcome"] for e in detail["audit"]]
        assert "duplicate" in outcomes and "rejected" in outcomes and "applied" in outcomes


# ---------------- 验收 4：暂停/刷新/恢复/复位后完整重放 ----------------

def test_acceptance_4_pause_refresh_resume_restore_replay():
    _reset_seed()
    with TestClient(app) as client:
        wo = _create(client)
        wid = wo["id"]
        _start(client, wo)
        _close(client, wid, 1, "V_TIN")
        _close(client, wid, 2, "V_TOUT")

        # 暂停于待核验状态
        r = _event(client, wid, "pause", key=f"{wid}:pause:1",
                   expected={"status": "pending_verification"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paused"

        # 暂停中禁止推进步骤
        r = _event(client, wid, "verify", key="verify-while-paused",
                   expected={"status": "paused", "step_seq": 3})
        assert r.status_code == 409, r.text

        # “刷新”：重新拉取详情 —— 状态、证据、原始拓扑全部重放
        detail = client.get(f"/api/work-orders/{wid}").json()
        assert detail["status"] == "paused"
        assert detail["resume_to"] == "pending_verification"
        assert [s["status"] for s in detail["steps"][:2]] == ["done", "done"]
        assert all(s["evidence"] for s in detail["steps"][:2])
        # 原始拓扑状态：冻结快照中所有阀门仍是打开、未锁定
        snap = detail["topology_snapshot"]
        assert all(v["is_open"] and not v["locked"] for v in snap["valves"])
        # 历史路径仍可重放：中断前的隔离状态与供给路径
        assert detail["live_check"]["isolated"] is True
        assert "BP" in detail["live_check"]["supply_paths"]["P2"]

        # 恢复 → 核验 → 复位
        r = _event(client, wid, "resume", key=f"{wid}:resume:1", expected={"status": "paused"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_verification"
        r = _event(client, wid, "verify", key=f"{wid}:step:3",
                   expected={"status": "pending_verification", "step_seq": 3})
        assert r.status_code == 200, r.text
        verify_evidence = r.json()["evidence"]
        for seq, valve in ((4, "V_TOUT"), (5, "V_TIN")):
            r = _event(client, wid, "restore_valve", key=f"{wid}:step:{seq}", valve_id=valve,
                       expected={"status": "restoring", "step_seq": seq, "valve_is_open": False})
            assert r.status_code == 200, r.text
        assert r.json()["status"] == "closed"

        # 复位后：模拟阀态回到冻结的原始状态，证据与历史路径完整保留
        detail = client.get(f"/api/work-orders/{wid}").json()
        assert detail["status"] == "closed"
        assert all(detail["simulated_states"].values())          # 全部恢复打开
        frozen_open = {v["id"]: v["is_open"] for v in snap["valves"]}
        assert detail["simulated_states"] == frozen_open         # 与冻结阀态一致
        assert detail["planned_supply_paths"]["P2"] is not None  # 历史路径保留
        verify_step = next(s for s in detail["steps"] if s["kind"] == "verify")
        assert verify_step["evidence"]["verified"] is True
        assert verify_step["evidence"]["supply_paths"] == verify_evidence["supply_paths"]
        # 完整审计链：创建→开始→关阀→暂停→恢复→核验→复位→关闭
        actions = [e["action"] for e in detail["audit"] if e["outcome"] == "applied"]
        assert actions == [
            "create", "start", "close_valve", "close_valve",
            "pause", "resume", "verify", "restore_valve", "restore_valve",
        ]


# ---------------- 补充：创建校验与幂等边界 ----------------

def test_create_rejects_invalid_plan():
    _reset_seed()
    with TestClient(app) as client:
        # 只关一侧不能隔离 → 400
        r = client.post("/api/work-orders",
                        json={"target_id": "T", "close_valves": ["V_TIN"]})
        assert r.status_code == 400
        # 未知阀门 → 400
        r = client.post("/api/work-orders",
                        json={"target_id": "T", "close_valves": ["NOPE"]})
        assert r.status_code == 400
        # 锁定阀门不能入选方案
        client.post("/api/valves/V_TOUT/lock", json={"locked": True})
        r = client.post("/api/work-orders",
                        json={"target_id": "T", "close_valves": ["V_TIN", "V_TOUT"]})
        assert r.status_code == 400
        # 未知目标 → 400；未知工单 → 404
        r = client.post("/api/work-orders",
                        json={"target_id": "NOPE", "close_valves": ["V_TIN"]})
        assert r.status_code == 400
        assert client.get("/api/work-orders/WO-NOPE").status_code == 404


def test_reset_with_constraint_change_expires_draft():
    """草稿工单在现场约束变化后同样不允许沿用旧方案开工。"""
    _reset_seed()
    with TestClient(app) as client:
        wo = _create(client)
        client.post("/api/valves/V1/lock", json={"locked": True})
        r = _event(client, wo["id"], "start", key="k-start", expected={"status": "draft"})
        assert r.status_code == 409, r.text
        detail = client.get(f"/api/work-orders/{wo['id']}").json()
        assert detail["status"] == "expired"
