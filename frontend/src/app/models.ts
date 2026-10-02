export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface Segment {
  id: string;
  source: string;
  target: string;
  direction: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve_id: string | null;
}

export interface Valve {
  id: string;
  name: string;
  segment_id: string;
  endpoints: string[];
  is_open: boolean;
  locked: boolean;
  operable: boolean;
  is_bypass: boolean;
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  examined_combinations: number;
  solutions: Solution[];
  best_solution: string[];
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
}

// ---------------- 隔离演练工单 ----------------

export type WorkOrderStatus =
  | 'draft'
  | 'executing'
  | 'paused'
  | 'pending_verification'
  | 'restoring'
  | 'closed'
  | 'stale';

export type StepKind = 'close_valve' | 'verify' | 'restore_valve';

export interface WorkOrderStep {
  seq: number;
  kind: StepKind;
  valve_id: string | null;
  status: 'pending' | 'done';
  evidence: Record<string, unknown> | null;
}

export interface WorkOrderEvent {
  seq: number;
  type: string;
  result: 'accepted' | 'duplicate' | 'rejected';
  detail: string;
  idempotency_key: string | null;
  expected_status: string | null;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

export interface SimulationView {
  engine: string;
  basis: string;
  simulated_closed_valves: string[];
  isolation_ok: boolean;
  essentials_supplied: string[];
  essentials_all_supplied: boolean;
  supply_paths: Record<string, string[] | null>;
  valve_states: Record<string, string>;
}

export interface WorkOrderPlan {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface WorkOrderSnapshot {
  sources: string[];
  essentials: string[];
  nodes: { id: string; name: string; kind: string; essential: boolean }[];
  valves: {
    id: string;
    name: string;
    segment_id: string;
    is_open: boolean;
    locked: boolean;
    operable: boolean;
  }[];
}

export interface WorkOrder {
  id: string;
  title: string;
  target_id: string;
  status: WorkOrderStatus;
  resume_status: WorkOrderStatus | null;
  disclaimer: string;
  fingerprint: string;
  plan: WorkOrderPlan;
  snapshot: WorkOrderSnapshot;
  steps: WorkOrderStep[];
  events: WorkOrderEvent[];
  simulation: SimulationView;
  created_at: string | null;
  updated_at: string | null;
  last_event?: { seq: number; result: string; duplicate: boolean };
}
