import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  IsolationResult,
  Topology,
  WorkOrderAction,
  WorkOrderDetail,
  WorkOrderEventResult,
  WorkOrderSummary,
} from './models';

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
    closeValves: string[],
    title?: string,
  ): Observable<WorkOrderDetail> {
    return this.http.post<WorkOrderDetail>('/api/work-orders', {
      target_id: targetId,
      close_valves: closeValves,
      title: title ?? null,
    });
  }

  listWorkOrders(): Observable<WorkOrderSummary[]> {
    return this.http.get<WorkOrderSummary[]>('/api/work-orders');
  }

  getWorkOrder(id: string): Observable<WorkOrderDetail> {
    return this.http.get<WorkOrderDetail>(`/api/work-orders/${id}`);
  }

  postWorkOrderEvent(
    id: string,
    action: WorkOrderAction,
    idempotencyKey: string,
    expected: Record<string, unknown>,
    valveId?: string,
  ): Observable<WorkOrderEventResult> {
    return this.http.post<WorkOrderEventResult>(`/api/work-orders/${id}/events`, {
      action,
      valve_id: valveId ?? null,
      idempotency_key: idempotencyKey,
      expected,
    });
  }
}
