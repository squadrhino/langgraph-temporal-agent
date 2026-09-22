import { DecimalPipe, PercentPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';

import { StoreService } from '../core/store.service';

/**
 * Stacked share bar + interactive legend. The legend doubles as the status
 * filter, and every segment carries a text label so meaning never depends on
 * colour alone.
 */
@Component({
  selector: 'app-status-distribution',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe, PercentPipe],
  template: `
    <section class="card dist" aria-labelledby="dist-title">
      <div class="card__head">
        <h2 id="dist-title">Status distribution</h2>
        <p class="card__note">Select a status to filter the table</p>
      </div>

      <div class="dist__body">
        @if (store.loading()) {
          <span class="skeleton bar-skeleton"></span>
        } @else if (slices().length) {
          <div class="bar" role="img" [attr.aria-label]="summary()">
            @for (slice of slices(); track slice.status) {
              <button
                type="button"
                class="bar__seg"
                [class.bar__seg--dim]="isDimmed(slice.status)"
                [style.width.%]="slice.share"
                [style.background]="'var(--st-' + slice.status + ')'"
                [attr.aria-pressed]="store.status() === slice.status"
                [title]="slice.status + ': ' + slice.count + ' orders'"
                (click)="toggle(slice.status)"
              >
                <span class="sr-only">{{ slice.status }}: {{ slice.count }} orders</span>
              </button>
            }
          </div>

          <ul class="legend">
            @for (slice of slices(); track slice.status) {
              <li>
                <button
                  type="button"
                  class="legend__item"
                  [class.legend__item--active]="store.status() === slice.status"
                  [attr.aria-pressed]="store.status() === slice.status"
                  (click)="toggle(slice.status)"
                >
                  <span class="legend__dot" [style.background]="'var(--st-' + slice.status + ')'"></span>
                  <span class="legend__name">{{ slice.status }}</span>
                  <span class="legend__count num">{{ slice.count | number }}</span>
                  <span class="legend__share num">{{ slice.share / 100 | percent: '1.0-0' }}</span>
                </button>
              </li>
            }
          </ul>
        } @else {
          <p class="dist__empty">No orders to summarise yet.</p>
        }
      </div>
    </section>
  `,
  styles: [
    `
      .dist__body {
        padding: var(--sp-4);
        display: flex;
        flex-direction: column;
        gap: var(--sp-3);
      }

      .card__note {
        font-size: 12px;
        color: var(--fg-subtle);
      }

      .bar-skeleton {
        display: block;
        height: 26px;
        border-radius: 99px;
      }

      .bar {
        display: flex;
        height: 26px;
        border-radius: 99px;
        overflow: hidden;
        background: var(--surface-2);
        gap: 2px;
      }

      .bar__seg {
        border: 0;
        padding: 0;
        min-width: 4px;
        cursor: pointer;
        transition: opacity var(--dur) var(--ease), filter var(--dur) var(--ease);

        &:hover {
          filter: brightness(1.12);
        }
      }

      .bar__seg--dim {
        opacity: 0.28;
      }

      .legend {
        display: flex;
        flex-wrap: wrap;
        gap: var(--sp-2);
        margin: 0;
        padding: 0;
        list-style: none;
      }

      .legend__item {
        display: inline-flex;
        align-items: center;
        gap: 7px;
        min-height: 32px;
        padding: 4px 10px;
        background: var(--surface-2);
        border: 1px solid var(--border);
        border-radius: 99px;
        font-size: 12px;
        cursor: pointer;
        transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);

        &:hover {
          border-color: var(--primary);
        }
      }

      .legend__item--active {
        border-color: var(--primary);
        background: var(--primary-soft);
      }

      .legend__dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
      }

      .legend__name {
        text-transform: capitalize;
        font-weight: 500;
      }

      .legend__count {
        font-weight: 600;
      }

      .legend__share {
        color: var(--fg-subtle);
      }

      .dist__empty {
        font-size: 13px;
        color: var(--fg-subtle);
      }
    `,
  ],
})
export class StatusDistributionComponent {
  protected readonly store = inject(StoreService);

  /**
   * The bar always describes the whole book of orders, so it keeps its shape
   * while a single status is selected (the other slices just dim).
   */
  protected readonly slices = computed(() => this.store.statusSlices());

  protected readonly summary = computed(() =>
    this.slices()
      .map((slice) => `${slice.status} ${slice.count}`)
      .join(', '),
  );

  protected isDimmed(status: string): boolean {
    const active = this.store.status();
    return active !== '' && active !== status;
  }

  protected toggle(status: string): void {
    const next = this.store.status() === status ? '' : status;
    this.store.setStatus(next as never);
  }
}
