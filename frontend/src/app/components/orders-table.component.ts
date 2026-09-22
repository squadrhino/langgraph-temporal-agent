import { CurrencyPipe, DatePipe, DecimalPipe, TitleCasePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, output } from '@angular/core';

import { StoreService } from '../core/store.service';
import { ORDER_STATUSES, OrderStatus, SortKey } from '../core/models';
import { IconComponent } from '../shared/icon.component';

@Component({
  selector: 'app-orders-table',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DatePipe, DecimalPipe, TitleCasePipe, IconComponent],
  templateUrl: './orders-table.component.html',
  styleUrl: './orders-table.component.scss',
})
export class OrdersTableComponent {
  /** Asks the host page to open the new-order dialog. */
  readonly newOrder = output<void>();

  protected readonly store = inject(StoreService);
  protected readonly statuses = ORDER_STATUSES;
  protected readonly columns: { key: SortKey; label: string; numeric?: boolean }[] = [
    { key: 'id', label: 'Order' },
    { key: 'customer_id', label: 'Customer' },
    { key: 'product', label: 'Product' },
    { key: 'quantity', label: 'Qty', numeric: true },
    { key: 'total', label: 'Total', numeric: true },
    { key: 'status', label: 'Status' },
    { key: 'created_at', label: 'Created' },
  ];
  protected readonly limits = [25, 50, 100, 200];
  /** Placeholder rows so the table keeps its height while loading. */
  protected readonly skeletonRows = Array.from({ length: 8 });

  protected readonly meta = computed(() => {
    if (this.store.loading()) {
      return 'Loading orders…';
    }
    const shown = this.store.visibleOrders().length;
    const loaded = this.store.orders().length;
    return shown === loaded
      ? `${loaded} orders`
      : `${shown} of ${loaded} orders match the current filters`;
  });

  /** Arrow shown next to the active sort column. */
  protected direction(key: SortKey): string {
    const sort = this.store.sort();
    if (sort.key !== key) {
      return '';
    }
    return sort.dir === 'asc' ? '▲' : '▼';
  }

  protected ariaSort(key: SortKey): 'ascending' | 'descending' | 'none' {
    const sort = this.store.sort();
    if (sort.key !== key) {
      return 'none';
    }
    return sort.dir === 'asc' ? 'ascending' : 'descending';
  }

  protected onSearch(event: Event): void {
    this.store.query.set((event.target as HTMLInputElement).value);
  }

  protected onStatus(event: Event): void {
    this.store.setStatus((event.target as HTMLSelectElement).value as OrderStatus | '');
  }

  protected onLimit(event: Event): void {
    this.store.setLimit(Number((event.target as HTMLSelectElement).value));
  }
}
