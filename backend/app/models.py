"""数据库模型：节点（设备/供给点）、管段、阀门、隔离演练工单。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """阀门与其所在管段一一对应（demo 模型）。

    is_open:       阀门当前实际开闭状态；锁定后用户不可再改变其操作状态。
    locked:        用户锁定标记，锁定阀门不参与候选关闭集合。
                   初始状态全部为打开、未锁定；当前演示中可操作的动作是“关闭”，
                   因此锁定等价于“禁止关闭”（保持打开）。
    operable:      工艺上是否允许操作（个别阀门检修/铅封，恒不可用）。
    """

    __tablename__ = "valves"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    segment: Mapped["Segment"] = relationship(back_populates="valve")


class Segment(Base):
    """有向管段：upstream -> downstream 表示介质名义流向。

    隔离计算按“物理连通”使用无向图（介质可被两侧隔离边界切断，
    且检修隔离关注连通性而非流向）；direction 用于前端箭头标注、
    来源/下游识别与结果解释。旁路是与主管并联的一对管段。
    """

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")  # main/bypass/branch
    is_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    valve: Mapped["Valve | None"] = relationship(back_populates="segment", uselist=False)

    __table_args__ = (UniqueConstraint("upstream_id", "downstream_id", name="uq_segment_endpoints"),)


class DrillWorkOrder(Base):
    """隔离演练工单：把一次隔离方案冻结为可暂停、可核验、可恢复的模拟流程。

    冻结内容（创建后不再随现场变化）：
    - target_id / sources / essentials：目标与必要供给点；
    - planned_close_valves / planned_supply_paths：所选方案及其供给路径；
    - lock_snapshot / topology_snapshot：锁定状态与完整拓扑快照；
    - fingerprint：锁定 + 拓扑 + 供给约束的哈希，执行中一旦与现场不符即过期。

    simulated_states 是工单自己的“模拟阀态”，步骤只改它、不写真实阀门表，
    因此工单全程只是流程模拟，不连接任何真实控制系统。
    """

    __tablename__ = "drill_work_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # draft/executing/paused/pending_verification/restoring/closed/expired
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    resume_to: Mapped[str | None] = mapped_column(String(24), nullable=True)
    sources: Mapped[list] = mapped_column(JSON, nullable=False)
    essentials: Mapped[list] = mapped_column(JSON, nullable=False)
    planned_close_valves: Mapped[list] = mapped_column(JSON, nullable=False)
    planned_supply_paths: Mapped[dict] = mapped_column(JSON, nullable=False)
    lock_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    topology_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    simulated_states: Mapped[dict] = mapped_column(JSON, nullable=False)
    expired_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, onupdate=_utcnow
    )

    steps: Mapped[list["DrillStep"]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan", order_by="DrillStep.seq"
    )
    events: Mapped[list["DrillEvent"]] = relationship(
        back_populates="work_order", cascade="all, delete-orphan", order_by="DrillEvent.id"
    )


class DrillStep(Base):
    """工单计划步骤：模拟关阀 / 核验 / 模拟复位。

    expected_pre 记录该步骤要求的前置状态（工单状态、阀态），
    执行时服务端按实际状态复核；evidence 为完成时固化的证据。
    """

    __tablename__ = "drill_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_id: Mapped[str] = mapped_column(
        ForeignKey("drill_work_orders.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # close_valve/verify/restore_valve
    valve_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/done
    expected_pre: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    work_order: Mapped[DrillWorkOrder] = relationship(back_populates="steps")


class DrillEvent(Base):
    """持久化审计：工单上发生的一切（应用/重复/拒绝/过期/状态迁移）。

    outcome: applied（已应用）/ duplicate（幂等重放）/ rejected（安全拒绝）/
    expired（约束变化导致过期）。拒绝与重复同样落库，保证乱序、重复
    提交可追溯。
    """

    __tablename__ = "drill_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_order_id: Mapped[str] = mapped_column(
        ForeignKey("drill_work_orders.id"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    work_order: Mapped[DrillWorkOrder] = relationship(back_populates="events")
