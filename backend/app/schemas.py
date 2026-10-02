"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )


class NodeOut(BaseModel):
    id: str
    name: str
    kind: str
    x: float
    y: float
    essential: bool


class ValveOut(BaseModel):
    id: str
    name: str
    segment_id: str
    endpoints: list[str]
    is_open: bool
    locked: bool
    operable: bool
    is_bypass: bool


class SegmentOut(BaseModel):
    id: str
    source: str
    target: str
    direction: str
    kind: str
    is_bypass: bool
    valve_id: str | None


class TopologyOut(BaseModel):
    nodes: list[NodeOut]
    segments: list[SegmentOut]
    valves: list[ValveOut]


class IsolationSolution(BaseModel):
    close_valves: list[str]
    size: int
    alternative_rank: int
    closes_bypass_valves: list[str] = []
    supply_paths: dict[str, list[str] | None] = {}


class ResidualPath(BaseModel):
    nodes: list[str]
    valves: list[str | None]
    locked_valves_on_path: list[str]
    uses_bypass: bool


class UnconstrainedBest(BaseModel):
    close_valves: list[str]
    size: int
    disconnects_essentials: list[str]
    unavoidable_essentials: list[str] = []


class IsolationOut(BaseModel):
    feasible: bool
    target_id: str
    sources: list[str]
    essentials: list[str]
    candidate_valves: list[ValveOut]
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None


class WorkOrderCreateIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}，随工单一起冻结"
    )
    solution_rank: int = Field(0, ge=0, description="选用的等价方案序号（0=推荐方案）")
    title: str | None = Field(None, description="工单标题，缺省自动生成")


class WorkOrderEventIn(BaseModel):
    type: str = Field(
        ..., description="start/close_valve/verify/pause/resume/restore_valve"
    )
    idempotency_key: str = Field(
        ..., min_length=1, max_length=64,
        description="幂等标识：同一工单内相同 key 的重复提交幂等回放，不重复执行步骤",
    )
    expected_status: str = Field(
        ..., description="前置状态：客户端声明提交时看到的工单状态，不符则拒绝"
    )
    valve_id: str | None = Field(None, description="close_valve/restore_valve 事件的阀门 id")
    note: str | None = Field(None, description="可选备注，随证据一起保存")
