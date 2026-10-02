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
  | 'expired';

export type WorkOrderAction =
  | 'start'
  | 'close_valve'
  | 'verify'
  | 'restore_valve'
  | 'pause'
  | 'resume';

export interface WorkOrderStep {
  seq: number;
  kind: 'close_valve' | 'verify' | 'restore_valve';
  valve_id: string | null;
  status: 'pending' | 'done';
  expected_pre: Record<string, unknown>;
  idempotency_key: string | null;
  evidence: Record<string, any> | null;
  completed_at: string | null;
}

export interface LiveCheck {
  isolated: boolean;
  all_essentials_supplied: boolean;
  supply_paths: Record<string, string[] | null>;
  residual_path: string[] | null;
  closed_valves: string[];
}

export interface AuditEntry {
  id: number;
  ts: string | null;
  action: string;
  outcome: 'applied' | 'duplicate' | 'rejected' | 'expired' | string;
  idempotency_key: string | null;
  detail: Record<string, any>;
}

export interface WorkOrderSummary {
  id: string;
  title: string;
  target_id: string;
  status: WorkOrderStatus;
  planned_close_valves: string[];
  steps_total: number;
  steps_done: number;
  expired_reason: string | null;
  created_at: string | null;
}

export interface WorkOrderDetail extends WorkOrderSummary {
  resume_to: WorkOrderStatus | null;
  sources: string[];
  essentials: string[];
  planned_supply_paths: Record<string, string[] | null>;
  lock_snapshot: Record<string, boolean>;
  simulated_states: Record<string, boolean>;
  topology_snapshot: Topology;
  fingerprint: string;
  steps: WorkOrderStep[];
  next_step: { seq: number; kind: WorkOrderStep['kind']; valve_id: string | null } | null;
  live_check: LiveCheck;
  audit: AuditEntry[];
  applied_event_count: number;
  disclaimer: string;
  updated_at: string | null;
}

export interface WorkOrderEventResult {
  outcome: 'applied' | 'duplicate';
  work_order_id: string;
  status: WorkOrderStatus;
  action?: WorkOrderAction;
  step?: { seq: number; kind: string; valve_id: string | null } | null;
  evidence?: Record<string, any> | null;
  live_check?: LiveCheck;
  recorded?: Record<string, any>;
}
