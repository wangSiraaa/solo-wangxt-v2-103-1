import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { IsolationResult, Topology, WorkOrder } from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }

  isolate(targetId: string, locks?: Record<string, boolean>): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
    });
  }

  // ---------------- 隔离演练工单 ----------------

  createWorkOrder(
    targetId: string,
    locks: Record<string, boolean> | null,
    solutionRank = 0,
  ): Observable<WorkOrder> {
    return this.http.post<WorkOrder>('/api/workorders', {
      target_id: targetId,
      locks,
      solution_rank: solutionRank,
      title: null,
    });
  }

  workOrder(id: string): Observable<WorkOrder> {
    return this.http.get<WorkOrder>(`/api/workorders/${id}`);
  }

  sendWorkOrderEvent(
    id: string,
    type: string,
    expectedStatus: string,
    valveId?: string,
  ): Observable<WorkOrder> {
    return this.http.post<WorkOrder>(`/api/workorders/${id}/events`, {
      type,
      // 每次提交生成新的幂等标识；重发同一标识可安全幂等回放
      idempotency_key: crypto.randomUUID(),
      expected_status: expectedStatus,
      valve_id: valveId ?? null,
      note: null,
    });
  }
}
