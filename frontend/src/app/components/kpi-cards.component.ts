import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { StoreService } from '../core/store.service';
import { IconComponent } from '../shared/icon.component';

@Component({
  selector: 'app-kpi-cards',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CurrencyPipe, DecimalPipe, IconComponent],
  template: `
    <section class="kpis" aria-label="Key metrics">
      <article class="kpi">
        <span class="kpi__icon kpi__icon--blue"><app-icon name="inbox" /></span>
        <div class="kpi__body">
          <p class="kpi__label">Orders loaded</p>
          @if (store.loading()) {
            <span class="skeleton kpi__skeleton"></span>
          } @else {
            <p class="kpi__value num">{{ store.kpis().total | number }}</p>
          }
          <p class="kpi__hint">{{ store.kpis().units | number }} units across all rows</p>
        </div>
      </article>

      <article class="kpi">
        <span class="kpi__icon kpi__icon--green"><app-icon name="cash" /></span>
        <div class="kpi__body">
          <p class="kpi__label">Booked revenue</p>
          @if (store.loading()) {
            <span class="skeleton kpi__skeleton"></span>
          } @else {
            <p class="kpi__value num">{{ store.kpis().revenue | currency: 'USD' : 'symbol' : '1.0-0' }}</p>
          }
          <p class="kpi__hint">Cancelled and refunded excluded</p>
        </div>
      </article>

      <article class="kpi">
        <span class="kpi__icon kpi__icon--amber"><app-icon name="clock" /></span>
        <div class="kpi__body">
          <p class="kpi__label">Awaiting action</p>
          @if (store.loading()) {
            <span class="skeleton kpi__skeleton"></span>
          } @else {
            <p class="kpi__value num">{{ store.kpis().open | number }}</p>
          }
          <p class="kpi__hint">Pending, paid or processing</p>
        </div>
      </article>

      <article class="kpi">
        <span class="kpi__icon kpi__icon--violet"><app-icon name="gauge" /></span>
        <div class="kpi__body">
          <p class="kpi__label">Average order value</p>
          @if (store.loading()) {
            <span class="skeleton kpi__skeleton"></span>
          } @else {
            <p class="kpi__value num">
              {{ store.kpis().averageValue | currency: 'USD' : 'symbol' : '1.2-2' }}
            </p>
          }
          <p class="kpi__hint">Over {{ store.kpis().bookedCount | number }} booked orders</p>
        </div>
      </article>
    </section>
  `,
  styles: [
    `
      .kpis {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
        gap: var(--sp-3);
      }

      .kpi {
        display: flex;
        align-items: flex-start;
        gap: var(--sp-3);
        padding: var(--sp-4);
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--r-lg);
        box-shadow: var(--shadow-1);
      }

      .kpi__icon {
        display: grid;
        place-items: center;
        width: 36px;
        height: 36px;
        border-radius: var(--r-md);
        --icon-size: 19px;
      }

      .kpi__icon--blue {
        background: color-mix(in srgb, var(--st-paid) 14%, transparent);
        color: var(--st-paid);
      }
      .kpi__icon--green {
        background: color-mix(in srgb, var(--st-delivered) 14%, transparent);
        color: var(--st-delivered);
      }
      .kpi__icon--amber {
        background: color-mix(in srgb, var(--st-pending) 16%, transparent);
        color: var(--st-pending);
      }
      .kpi__icon--violet {
        background: color-mix(in srgb, var(--st-processing) 16%, transparent);
        color: var(--st-processing);
      }

      .kpi__body {
        min-width: 0;
      }

      .kpi__label {
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        color: var(--fg-muted);
      }

      .kpi__value {
        font-size: 26px;
        font-weight: 600;
        line-height: 1.25;
      }

      .kpi__skeleton {
        display: block;
        width: 92px;
        height: 30px;
        margin: 2px 0;
      }

      .kpi__hint {
        font-size: 12px;
        color: var(--fg-subtle);
      }
    `,
  ],
})
export class KpiCardsComponent {
  protected readonly store = inject(StoreService);
}
