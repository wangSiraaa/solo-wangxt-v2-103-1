import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { IsolationResult, StepKind, Topology, WorkOrder, WorkOrderStep } from './models';
import { NetworkGraphComponent } from './network-graph.component';

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

  // 隔离演练工单（当前打开的一张；历史工单持久化在后端）
  workOrder = signal<WorkOrder | null>(null);
  woBusy = signal(false);

  /** 工单模拟状态 -> 拓扑图高亮（核验前即可见目标隔离与必要供给路径）。 */
  woSimClosed = computed(() => this.workOrder()?.simulation.simulated_closed_valves ?? []);
  woSimSupply = computed(() => this.workOrder()?.simulation.supply_paths ?? {});

  readonly STATUS_LABELS: Record<string, string> = {
    draft: '草稿',
    executing: '执行中',
    paused: '已暂停',
    pending_verification: '待核验',
    restoring: '恢复中',
    closed: '已关闭',
    stale: '已过期',
  };
  readonly STEP_LABELS: Record<StepKind, string> = {
    close_valve: '模拟关阀',
    verify: '核验（NetworkX 复核）',
    restore_valve: '模拟复位',
  };

  ngOnInit(): void {
    this.reload();
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
        this.error.set(`计算失败：${e.error?.detail ?? e.message ?? e}`);
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
    });
  }

  // ---------------- 隔离演练工单 ----------------

  /** 把当前可行计算结果冻结为一张演练工单（草稿）。 */
  createWorkOrder(): void {
    const r = this.result();
    if (!r || !r.feasible) {
      return;
    }
    this.woBusy.set(true);
    this.error.set(null);
    this.api.createWorkOrder(this.targetId(), this.locks(), 0).subscribe({
      next: (wo) => {
        this.workOrder.set(wo);
        this.woBusy.set(false);
      },
      error: (e) => {
        this.error.set(`创建工单失败：${e.error?.detail ?? e.message ?? e}`);
        this.woBusy.set(false);
      },
    });
  }

  /** 提交工单事件；expected_status 取当前看到的状态作为前置状态。 */
  woEvent(type: string, valveId?: string): void {
    const wo = this.workOrder();
    if (!wo) {
      return;
    }
    this.woBusy.set(true);
    this.error.set(null);
    this.api.sendWorkOrderEvent(wo.id, type, wo.status, valveId).subscribe({
      next: (w) => {
        this.workOrder.set(w);
        this.woBusy.set(false);
      },
      error: (e) => {
        // 409 等拒绝已写入后端审计；重新拉取以同步状态（如过期）与审计链
        this.error.set(`工单事件被拒绝：${e.error?.detail ?? e.message ?? e}`);
        this.woBusy.set(false);
        this.refreshWorkOrder();
      },
    });
  }

  /** 刷新工单（模拟页面刷新：从持久化存储重读，含活体过期检查）。 */
  refreshWorkOrder(): void {
    const wo = this.workOrder();
    if (!wo) {
      return;
    }
    this.api.workOrder(wo.id).subscribe({
      next: (w) => this.workOrder.set(w),
      error: () => this.workOrder.set(null),
    });
  }

  discardWorkOrder(): void {
    this.workOrder.set(null);
  }

  nextStep(kind: StepKind): WorkOrderStep | null {
    const wo = this.workOrder();
    if (!wo) {
      return null;
    }
    return wo.steps.find((s) => s.kind === kind && s.status === 'pending') ?? null;
  }

  verifyEvidence(wo: WorkOrder): Record<string, unknown> | null {
    return wo.steps.find((s) => s.kind === 'verify' && s.evidence)?.evidence ?? null;
  }

  statusLabel(status: string): string {
    return this.STATUS_LABELS[status] ?? status;
  }

  stepLabel(kind: StepKind): string {
    return this.STEP_LABELS[kind] ?? kind;
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
