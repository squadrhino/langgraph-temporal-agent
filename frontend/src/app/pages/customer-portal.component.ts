import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { AgentChatComponent } from '../components/agent-chat.component';
import { CUSTOMER_ID_PATTERN, CustomerSessionService } from '../core/customer-session.service';
import { ThemeService } from '../core/theme.service';
import { IconComponent } from '../shared/icon.component';

/**
 * Customer-facing counterpart to the staff console: identify yourself with a
 * customer id, then talk to support.
 *
 * The chat posts to the same `POST /agent` endpoint the console uses. That
 * endpoint is a stub today, so the page says so plainly instead of pretending
 * to answer; when the agent is wired up, this page needs no changes beyond
 * passing the customer id along with the message.
 */
@Component({
  selector: 'app-customer-portal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, RouterLink, AgentChatComponent, IconComponent],
  templateUrl: './customer-portal.component.html',
  styleUrl: './customer-portal.component.scss',
})
export class CustomerPortalComponent {
  private readonly fb = inject(FormBuilder);
  protected readonly session = inject(CustomerSessionService);
  protected readonly theme = inject(ThemeService);

  protected readonly submitted = signal(false);

  protected readonly form = this.fb.nonNullable.group({
    customer_id: ['', [Validators.required, Validators.pattern(CUSTOMER_ID_PATTERN)]],
  });

  protected readonly suggestions = [
    'Where is my latest order?',
    'My order arrived damaged — can I get a refund?',
    'Can I change the delivery address?',
  ];

  protected readonly greeting = computed(() => {
    const customerId = this.session.customerId();
    return customerId ? `Signed in as ${customerId}` : '';
  });

  protected get invalid(): boolean {
    const control = this.form.controls.customer_id;
    return control.invalid && (control.touched || this.submitted());
  }

  protected signIn(): void {
    this.submitted.set(true);
    if (this.form.invalid) {
      document.getElementById('customer-id')?.focus();
      return;
    }
    this.session.signIn(this.form.getRawValue().customer_id);
  }

  protected signOut(): void {
    this.session.signOut();
    this.submitted.set(false);
    this.form.reset({ customer_id: '' });
  }
}
