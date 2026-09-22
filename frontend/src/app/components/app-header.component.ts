import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { DatePipe } from '@angular/common';

import { AuthService } from '../core/auth.service';
import { StoreService } from '../core/store.service';
import { ThemeService } from '../core/theme.service';
import { IconComponent } from '../shared/icon.component';

@Component({
  selector: 'app-header',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, IconComponent],
  template: `
    <header class="bar">
      <div class="brand">
        <span class="brand__mark" aria-hidden="true">
          <app-icon name="cart" />
        </span>
        <div class="brand__text">
          <h1>Computer Store</h1>
          <p>Operations console</p>
        </div>
      </div>

      <div class="bar__actions">
        <span class="pill" [class]="'pill--' + state()" role="status">
          <app-icon [name]="state() === 'up' ? 'check' : 'alert'" />
          <span>{{ label() }}</span>
        </span>

        @if (store.lastUpdated(); as updated) {
          <span class="stamp num" title="Time of the last successful /orders call">
            {{ updated | date: 'HH:mm:ss' }}
          </span>
        }

        @if (auth.isAuthenticated()) {
          <span class="who" title="Identity from the access token, verified by the API">
            <strong>{{ auth.user()?.username }}</strong>
            <span class="who__role">{{ roleLabel() }}</span>
          </span>
          <button class="btn btn--ghost btn--sm" type="button" (click)="auth.logout()">
            Sign out
          </button>
        } @else {
          <button class="btn btn--sm" type="button" (click)="auth.login()">Sign in</button>
        }

        <a class="btn btn--ghost btn--sm" href="/docs" target="_blank" rel="noopener">
          API docs <app-icon name="external" />
        </a>

        <button
          class="btn btn--ghost btn--icon"
          type="button"
          (click)="theme.toggle()"
          [attr.aria-label]="
            theme.theme() === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'
          "
        >
          <app-icon [name]="theme.theme() === 'dark' ? 'sun' : 'moon'" />
        </button>
      </div>
    </header>
  `,
  styles: [
    `
      .bar {
        position: sticky;
        top: 0;
        z-index: 20;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--sp-4);
        flex-wrap: wrap;
        padding: var(--sp-3) var(--sp-5);
        background: color-mix(in srgb, var(--bg) 82%, transparent);
        backdrop-filter: blur(10px);
        border-bottom: 1px solid var(--border);
      }

      .brand {
        display: flex;
        align-items: center;
        gap: var(--sp-3);
      }

      .brand__mark {
        display: grid;
        place-items: center;
        width: 38px;
        height: 38px;
        border-radius: var(--r-md);
        background: var(--primary-soft);
        color: var(--primary);
        --icon-size: 20px;
      }

      h1 {
        font-size: 17px;
        font-weight: 700;
        letter-spacing: -0.01em;
      }

      .brand__text p {
        font-size: 12px;
        color: var(--fg-subtle);
      }

      .bar__actions {
        display: flex;
        align-items: center;
        gap: var(--sp-2);
      }

      .pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 5px 10px;
        border-radius: 99px;
        border: 1px solid var(--border-strong);
        font-size: 12px;
        font-weight: 600;
        --icon-size: 14px;
      }

      .pill--up {
        color: var(--success);
        border-color: color-mix(in srgb, var(--success) 40%, transparent);
        background: color-mix(in srgb, var(--success) 12%, transparent);
      }

      .pill--down {
        color: var(--danger);
        border-color: color-mix(in srgb, var(--danger) 40%, transparent);
        background: var(--danger-soft);
      }

      .stamp {
        font-size: 12px;
        color: var(--fg-subtle);
      }

      .who {
        display: inline-flex;
        flex-direction: column;
        line-height: 1.25;
        font-size: 12px;
        padding-right: var(--sp-1);
      }

      .who strong {
        font-weight: 650;
      }

      .who__role {
        color: var(--fg-subtle);
        font-size: 11px;
      }

      @media (max-width: 640px) {
        .bar {
          padding: var(--sp-3);
        }
        .stamp {
          display: none;
        }
      }
    `,
  ],
})
export class AppHeaderComponent {
  protected readonly store = inject(StoreService);
  protected readonly theme = inject(ThemeService);
  protected readonly auth = inject(AuthService);

  /** The realm roles, minus Keycloak's own defaults, plus the customer scope. */
  protected readonly roleLabel = computed(() => {
    const user = this.auth.user();
    if (!user) return '';
    const roles = user.roles.filter(
      (r) => !r.startsWith('default-roles') && r !== 'offline_access' && r !== 'uma_authorization',
    );
    return user.customerId ? `${roles.join(', ')} · ${user.customerId}` : roles.join(', ');
  });

  protected readonly state = computed<'up' | 'down'>(() => (this.store.health() ? 'up' : 'down'));

  protected readonly label = computed(() => {
    const health = this.store.health();
    return health ? `API ${health.status} · ${health.orders} orders` : 'API unreachable';
  });
}
