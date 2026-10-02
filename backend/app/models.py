"""数据库模型：节点（设备/供给点）、管段、阀门、隔离演练工单。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


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


class WorkOrder(Base):
    """隔离演练工单：把一次隔离计算冻结为可暂停、可核验、可恢复的模拟流程。

    工单只模拟流程：snapshot_json 冻结创建时刻的拓扑/阀态/锁定/供给约束，
    plan_json 冻结所选方案；后续所有“关阀/核验/复位”都只在冻结快照上推演，
    绝不改动 Valve 表的真实状态，也不连接真实控制系统。
    fingerprint 是冻结前提的哈希，执行中前提一旦被改变（锁阀/拓扑/供给约束），
    工单即过期（stale）并阻止继续，避免沿用旧方案造成假完成。
    """

    __tablename__ = "work_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    # draft/executing/paused/pending_verification/restoring/closed/stale
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    resume_status: Mapped[str | None] = mapped_column(String(24), nullable=True)  # 暂停前状态
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    disclaimer: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    steps: Mapped[list["WorkOrderStep"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="WorkOrderStep.seq"
    )
    events: Mapped[list["WorkOrderEvent"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="WorkOrderEvent.seq"
    )


class WorkOrderStep(Base):
    """工单计划步骤：按序执行，不允许跳步；完成时写入证据（evidence_json）。"""

    __tablename__ = "work_order_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # close_valve/verify/restore_valve
    valve_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending/done
    evidence_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    order: Mapped[WorkOrder] = relationship(back_populates="steps")


class WorkOrderEvent(Base):
    """持久化审计日志：每一次事件提交（接受/幂等重复/拒绝）都留痕。

    idempotency_key 由调用方提供；同一工单内相同 key 的已接受事件直接幂等回放，
    重复提交不会重复执行步骤，但会以 result=duplicate 记录一条审计。
    """

    __tablename__ = "work_order_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("work_orders.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # 工单内单调递增
    type: Mapped[str] = mapped_column(String(24), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_status: Mapped[str | None] = mapped_column(String(24), nullable=True)  # 前置状态
    result: Mapped[str] = mapped_column(String(16), nullable=False)  # accepted/duplicate/rejected
    detail: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)

    order: Mapped[WorkOrder] = relationship(back_populates="events")
