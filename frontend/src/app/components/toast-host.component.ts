import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { ToastService } from '../core/toast.service';
import { IconComponent, IconName } from '../shared/icon.component';

@Component({
  selector: 'app-toast-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [IconComponent],
  template: `
    <div class="toasts" aria-live="polite" aria-atomic="false">
      @for (toast of toasts.toasts(); track toast.id) {
        <div class="toast" [class]="'toast--' + toast.kind" role="status">
          <app-icon [name]="icon(toast.kind)" class="toast__icon" />
          <div class="toast__text">
            <p class="toast__title">{{ toast.title }}</p>
            @if (toast.body) {
              <p class="toast__body">{{ toast.body }}</p>
            }
          </div>
          <button
            type="button"
            class="toast__close"
            aria-label="Dismiss notification"
            (click)="toasts.dismiss(toast.id)"
          >
            <app-icon name="close" />
          </button>
        </div>
      }
    </div>
  `,
  styles: [
    `
      /* Anchored under the header: bottom-right would sit on top of the
         agent composer's send button. */
      .toasts {
        position: fixed;
        right: var(--sp-4);
        top: 74px;
        z-index: 60;
        display: flex;
        flex-direction: column;
        gap: var(--sp-2);
        width: min(360px, calc(100vw - 2 * var(--sp-4)));
      }

      .toast {
        display: flex;
        align-items: flex-start;
        gap: var(--sp-3);
        padding: var(--sp-3);
        background: var(--surface);
        border: 1px solid var(--border-strong);
        border-left: 3px solid var(--tone, var(--primary));
        border-radius: var(--r-md);
        box-shadow: var(--shadow-2);
        animation: toast-in 200ms var(--ease);
      }

      .toast--success {
        --tone: var(--success);
      }
      .toast--error {
        --tone: var(--danger);
      }
      .toast--info {
        --tone: var(--primary);
      }

      .toast__icon {
        color: var(--tone);
        margin-top: 2px;
      }

      .toast__text {
        flex: 1;
        min-width: 0;
      }

      .toast__title {
        font-size: 14px;
        font-weight: 600;
      }

      .toast__body {
        font-size: 12.5px;
        color: var(--fg-muted);
        overflow-wrap: anywhere;
      }

      .toast__close {
        display: grid;
        place-items: center;
        width: 26px;
        height: 26px;
        padding: 0;
        border: 0;
        border-radius: var(--r-sm);
        background: transparent;
        color: var(--fg-subtle);
        cursor: pointer;
        --icon-size: 15px;

        &:hover {
          background: var(--surface-3);
          color: var(--fg);
        }
      }

      @keyframes toast-in {
        from {
          opacity: 0;
          transform: translateY(-8px);
        }
      }
    `,
  ],
})
export class ToastHostComponent {
  protected readonly toasts = inject(ToastService);

  protected icon(kind: string): IconName {
    if (kind === 'success') return 'check';
    if (kind === 'error') return 'alert';
    return 'info';
  }
}
