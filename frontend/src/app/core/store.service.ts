import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';

import { ApiService, describeError } from './api.service';
import {
  Health,
  OPEN_STATUSES,
  Order,
  OrderCreate,
  OrderStatus,
  Product,
  SortKey,
  SortState,
  VOID_STATUSES,
} from './models';
import { ToastService } from './toast.service';

export interface StatusSlice {
  status: OrderStatus;
  count: number;
  share: number;
}

/**
 * Single source of truth for the console.
 *
 * Status and limit are applied server-side (they are real query parameters on
 * `GET /orders`); free-text search and column sorting are applied client-side
 * over whatever the API returned.
 */
@Injectable({ providedIn: 'root' })
export class StoreService {
  private readonly api = inject(ApiService);
  private readonly toasts = inject(ToastService);

  // --- Raw state -----------------------------------------------------------
  readonly orders = signal<Order[]>([]);
  readonly products = signal<Product[]>([]);
  readonly health = signal<Health | null>(null);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly lastUpdated = signal<Date | null>(null);
  /** Order id to flash after it is created, so the new row is easy to spot. */
  readonly highlighted = signal<string | null>(null);

  // --- View state ----------------------------------------------------------
  readonly status = signal<OrderStatus | ''>('');
  readonly limit = signal(100);
  readonly query = signal('');
  readonly sort = signal<SortState>({ key: 'created_at', dir: 'desc' });

  // --- Derived -------------------------------------------------------------
  readonly visibleOrders = computed(() => {
    const status = this.status();
    const needle = this.query().trim().toLowerCase();
    const { key, dir } = this.sort();

    const rows = this.orders().filter((order) => {
      if (status && order.status !== status) {
        return false;
      }
      if (!needle) {
        return true;
      }
      return (
        order.id.toLowerCase().includes(needle) ||
        order.customer_id.toLowerCase().includes(needle) ||
        order.product.sku.toLowerCase().includes(needle) ||
        order.product.name.toLowerCase().includes(needle)
      );
    });

    const factor = dir === 'asc' ? 1 : -1;
    return rows.sort((a, b) => factor * compareOrders(a, b, key));
  });

  readonly hasFilters = computed(() => this.status() !== '' || this.query().trim() !== '');

  readonly kpis = computed(() => {
    const orders = this.orders();
    const booked = orders.filter((order) => !VOID_STATUSES.includes(order.status));
    const revenue = booked.reduce((sum, order) => sum + order.total, 0);
    const open = orders.filter((order) => OPEN_STATUSES.includes(order.status)).length;
    const units = orders.reduce((sum, order) => sum + order.quantity, 0);

    return {
      total: orders.length,
      revenue,
      open,
      units,
      averageValue: booked.length ? revenue / booked.length : 0,
      bookedCount: booked.length,
    };
  });

  readonly statusSlices = computed<StatusSlice[]>(() => {
    const orders = this.orders();
    const counts = new Map<OrderStatus, number>();
    for (const order of orders) {
      counts.set(order.status, (counts.get(order.status) ?? 0) + 1);
    }
    return [...counts.entries()]
      .map(([status, count]) => ({
        status,
        count,
        share: orders.length ? (count / orders.length) * 100 : 0,
      }))
      .sort((a, b) => b.count - a.count);
  });

  // --- Commands ------------------------------------------------------------
  init(): void {
    this.loadOrders();
    this.loadProducts();
    this.loadHealth();
  }

  refresh(): void {
    this.loadOrders();
    this.loadHealth();
  }

  setStatus(status: OrderStatus | ''): void {
    if (this.status() === status) {
      return;
    }
    this.status.set(status);
    this.loadOrders();
  }

  setLimit(limit: number): void {
    if (this.limit() === limit) {
      return;
    }
    this.limit.set(limit);
    this.loadOrders();
  }

  clearFilters(): void {
    this.query.set('');
    this.setStatus('');
  }

  /** Click the same column twice to flip the direction. */
  toggleSort(key: SortKey): void {
    this.sort.update((current) =>
      current.key === key
        ? { key, dir: current.dir === 'asc' ? 'desc' : 'asc' }
        : { key, dir: key === 'created_at' || key === 'total' ? 'desc' : 'asc' },
    );
  }

  createOrder(body: OrderCreate): Observable<Order> {
    return this.api.createOrder(body).pipe(
      tap((order) => {
        this.orders.update((all) => [order, ...all]);
        this.highlighted.set(order.id);
        setTimeout(() => {
          if (this.highlighted() === order.id) {
            this.highlighted.set(null);
          }
        }, 2500);
        this.loadHealth();
        this.toasts.success(
          `Order ${order.id} created`,
          `${order.quantity} x ${order.product.name} for ${order.customer_id}`,
        );
      }),
    );
  }

  private loadOrders(): void {
    this.loading.set(true);
    this.api.orders({ status: this.status(), limit: this.limit() }).subscribe({
      next: (orders) => {
        this.orders.set(orders);
        this.error.set(null);
        this.loading.set(false);
        this.lastUpdated.set(new Date());
      },
      error: (error) => {
        this.error.set(describeError(error));
        this.orders.set([]);
        this.loading.set(false);
      },
    });
  }

  private loadProducts(): void {
    this.api.products().subscribe({
      next: (products) => this.products.set(products),
      error: (error) => this.toasts.error('Could not load the catalogue', describeError(error)),
    });
  }

  private loadHealth(): void {
    this.api.health().subscribe({
      next: (health) => this.health.set(health),
      error: () => this.health.set(null),
    });
  }
}

function compareOrders(a: Order, b: Order, key: SortKey): number {
  switch (key) {
    case 'quantity':
      return a.quantity - b.quantity;
    case 'total':
      return a.total - b.total;
    case 'created_at':
      return Date.parse(a.created_at) - Date.parse(b.created_at);
    case 'product':
      return a.product.name.localeCompare(b.product.name);
    default:
      return String(a[key]).localeCompare(String(b[key]));
  }
}
