import { Component, OnInit, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import {
  IsolationResult,
  Topology,
  WorkOrderAction,
  WorkOrderDetail,
  WorkOrderStatus,
  WorkOrderStep,
  WorkOrderSummary,
} from './models';
import { NetworkGraphComponent } from './network-graph.component';

const STATUS_LABELS: Record<WorkOrderStatus, string> = {
  draft: '草稿',
  executing: '执行中',
  paused: '暂停',
  pending_verification: '待核验',
  restoring: '恢复中',
  closed: '关闭',
  expired: '已过期',
};

const STEP_KIND_LABELS: Record<WorkOrderStep['kind'], string> = {
  close_valve: '模拟关阀',
  verify: '核验',
  restore_valve: '模拟复位',
};

const OUTCOME_LABELS: Record<string, string> = {
  applied: '已应用',
  duplicate: '幂等重放',
  rejected: '安全拒绝',
  expired: '过期',
};

const ACTION_LABELS: Record<string, string> = {
  create: '创建工单',
  start: '开始执行',
  close_valve: '模拟关阀',
  verify: '核验',
  restore_valve: '模拟复位',
  pause: '暂停',
  resume: '恢复',
  'system.expire': '约束变化过期',
};

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);

  // 阀门锁定状态（仅前端选择，计算时随请求提交并由后端持久化）
  locks = signal<Record<string, boolean>>({});

  // 隔离演练工单
  workOrders = signal<WorkOrderSummary[]>([]);
  selectedWo = signal<WorkOrderDetail | null>(null);
  woBusy = signal(false);

  readonly statusLabels = STATUS_LABELS;
  readonly stepKindLabels = STEP_KIND_LABELS;
  readonly outcomeLabels = OUTCOME_LABELS;
  readonly actionLabels = ACTION_LABELS;

  ngOnInit(): void {
    this.reload();
    this.reloadWorkOrders();
  }

  reload(): void {
    this.loading.set(true);
    this.api.topology().subscribe({
      next: (t) => {
        this.topology.set(t);
        const l: Record<string, boolean> = {};
        for (const v of t.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`无法加载拓扑：${e.message ?? e}`);
        this.loading.set(false);
      },
    });
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.selectedWo.set(null);
    this.api.isolate(this.targetId(), this.locks()).subscribe({
      next: (r) => {
        this.result.set(r);
        // 同步锁定勾选状态（后端为权威来源）
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`计算失败：${this.errText(e)}`);
        this.loading.set(false);
      },
    });
  }

  /** 快捷装载三个培训样例的锁定组合（样例 1 先重置）。 */
  loadSample(sample: 1 | 2 | 3): void {
    const applyLocksAndCompute = () => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      if (sample === 2) {
        l['V_TIN'] = true;
      } else if (sample === 3) {
        l['V_TOUT'] = true;
      }
      this.locks.set(l);
      this.compute();
    };

    if (sample === 1) {
      // 重置 -> 重新拉取拓扑 -> 计算
      this.api.reset().subscribe(() => {
        this.api.topology().subscribe((t) => {
          this.topology.set(t);
          const l: Record<string, boolean> = {};
          for (const v of t.valves) {
            l[v.id] = v.locked;
          }
          this.locks.set(l);
          applyLocksAndCompute();
        });
      });
    } else {
      applyLocksAndCompute();
    }
  }

  resetAll(): void {
    this.api.reset().subscribe(() => {
      this.result.set(null);
      this.reload();
      this.reloadWorkOrders();
    });
  }

  // ---------------- 隔离演练工单 ----------------

  reloadWorkOrders(): void {
    this.api.listWorkOrders().subscribe({
      next: (list) => {
        this.workOrders.set(list);
        // 刷新后重放：localStorage 记住的选中工单重新拉取（状态/证据全部来自后端）
        const wanted = this.selectedWo()?.id ?? localStorage.getItem('selectedWoId');
        if (wanted && list.some((w) => w.id === wanted)) {
          this.selectWo(wanted);
        } else if (this.selectedWo() && !list.some((w) => w.id === this.selectedWo()!.id)) {
          this.selectedWo.set(null);
        }
      },
    });
  }

  createWorkOrder(): void {
    const r = this.result();
    if (!r?.feasible) {
      return;
    }
    this.woBusy.set(true);
    this.error.set(null);
    this.api.createWorkOrder(r.target_id, r.best_solution).subscribe({
      next: (wo) => {
        this.selectedWo.set(wo);
        localStorage.setItem('selectedWoId', wo.id);
        this.woBusy.set(false);
        this.reloadWorkOrders();
      },
      error: (e) => {
        this.error.set(`生成工单失败：${this.errText(e)}`);
        this.woBusy.set(false);
      },
    });
  }

  selectWo(id: string): void {
    this.api.getWorkOrder(id).subscribe({
      next: (wo) => {
        this.selectedWo.set(wo);
        localStorage.setItem('selectedWoId', wo.id);
      },
      error: () => this.selectedWo.set(null),
    });
  }

  deselectWo(): void {
    this.selectedWo.set(null);
    localStorage.removeItem('selectedWoId');
  }

  /** 提交工单事件；无论成功还是被安全拒绝，都重新拉取详情以重放最新状态与审计。 */
  private sendEvent(
    action: WorkOrderAction,
    expected: Record<string, unknown>,
    valveId?: string,
  ): void {
    const wo = this.selectedWo();
    if (!wo || this.woBusy()) {
      return;
    }
    this.woBusy.set(true);
    this.error.set(null);
    // 幂等标识：计划步骤按 seq 确定（重复提交同一键 → 服务端幂等重放）；
    // 控制动作附带已应用事件计数，双击/重试同一键不会重复应用。
    const key =
      action === 'close_valve' || action === 'verify' || action === 'restore_valve'
        ? `${wo.id}:step:${wo.next_step?.seq ?? 'x'}`
        : `${wo.id}:ctrl:${action}:${wo.applied_event_count}`;
    this.api.postWorkOrderEvent(wo.id, action, key, expected, valveId).subscribe({
      next: () => {
        this.woBusy.set(false);
        this.reloadWorkOrders();
      },
      error: (e) => {
        // 409：乱序/前置状态/过期被安全拒绝 —— 提示并刷新以展示审计记录
        this.error.set(`事件被拒绝：${this.errText(e)}`);
        this.woBusy.set(false);
        this.reloadWorkOrders();
      },
    });
  }

  woStart(): void {
    this.sendEvent('start', { status: 'draft' });
  }

  /** 执行下一个计划步骤（模拟关阀 / 核验 / 模拟复位）。 */
  woNextStep(): void {
    const wo = this.selectedWo();
    const ns = wo?.next_step;
    if (!wo || !ns) {
      return;
    }
    if (ns.kind === 'close_valve') {
      this.sendEvent(
        'close_valve',
        { status: wo.status, step_seq: ns.seq, valve_is_open: true },
        ns.valve_id ?? undefined,
      );
    } else if (ns.kind === 'verify') {
      this.sendEvent('verify', { status: wo.status, step_seq: ns.seq });
    } else {
      this.sendEvent(
        'restore_valve',
        { status: wo.status, step_seq: ns.seq, valve_is_open: false },
        ns.valve_id ?? undefined,
      );
    }
  }

  woPause(): void {
    const wo = this.selectedWo();
    if (wo) {
      this.sendEvent('pause', { status: wo.status });
    }
  }

  woResume(): void {
    this.sendEvent('resume', { status: 'paused' });
  }

  nextStepLabel(): string {
    const wo = this.selectedWo();
    const ns = wo?.next_step;
    if (!wo || !ns) {
      return '';
    }
    if (ns.kind === 'verify') {
      return `核验（步骤 ${ns.seq}）`;
    }
    const name = ns.valve_id ? this.valveName(ns.valve_id) : '';
    const verb = ns.kind === 'close_valve' ? '模拟关阀' : '模拟复位';
    return `${verb} ${ns.valve_id}（${name}，步骤 ${ns.seq}）`;
  }

  canPause(wo: WorkOrderDetail): boolean {
    return ['executing', 'pending_verification', 'restoring'].includes(wo.status);
  }

  canStep(wo: WorkOrderDetail): boolean {
    return (
      wo.next_step !== null &&
      ['executing', 'pending_verification', 'restoring'].includes(wo.status)
    );
  }

  woValves(wo: WorkOrderDetail): string[] {
    return Object.keys(wo.simulated_states);
  }

  hasLockedValves(wo: WorkOrderDetail): boolean {
    return Object.values(wo.lock_snapshot).some((locked) => locked);
  }

  private errText(e: any): string {
    const d = e?.error?.detail;
    if (typeof d === 'string') {
      return d;
    }
    if (d && typeof d === 'object') {
      return d.message ?? JSON.stringify(d);
    }
    return e?.message ?? String(e);
  }

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  nodeName(id: string): string {
    return this.topology()?.nodes.find((n) => n.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }
}
