import { CurrencyPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, output, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { toSignal } from '@angular/core/rxjs-interop';

import { describeError } from '../core/api.service';
import { CATEGORY_LABELS, ProductCategory } from '../core/models';
import { StoreService } from '../core/store.service';
import { ToastService } from '../core/toast.service';
import { IconComponent } from '../shared/icon.component';

/** Validation mirrors the Pydantic constraints on `OrderCreate`. */
@Component({
  selector: 'app-order-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, CurrencyPipe, IconComponent],
  templateUrl: './order-form.component.html',
  styleUrl: './order-form.component.scss',
})
export class OrderFormComponent {
  /** Emitted after the API accepts an order, so a dialog host can close. */
  readonly created = output<void>();

  private readonly fb = inject(FormBuilder);
  private readonly toasts = inject(ToastService);
  protected readonly store = inject(StoreService);

  protected readonly submitting = signal(false);

  protected readonly form = this.fb.nonNullable.group({
    customer_id: ['', [Validators.required, Validators.pattern(/\S/)]],
    product_sku: ['', Validators.required],
    quantity: [1, [Validators.required, Validators.min(1), Validators.max(10)]],
  });

  private readonly value = toSignal(this.form.valueChanges, { initialValue: this.form.getRawValue() });

  protected readonly selected = computed(() =>
    this.store.products().find((product) => product.sku === this.value().product_sku),
  );

  protected readonly total = computed(() => {
    const product = this.selected();
    const quantity = Number(this.value().quantity ?? 0);
    if (!product || !Number.isFinite(quantity) || quantity < 1) {
      return 0;
    }
    return product.price * quantity;
  });

  protected categoryLabel(category: ProductCategory): string {
    return CATEGORY_LABELS[category];
  }

  protected invalid(name: 'customer_id' | 'product_sku' | 'quantity'): boolean {
    const control = this.form.controls[name];
    return control.invalid && (control.touched || control.dirty);
  }

  protected submit(): void {
    if (this.submitting()) {
      return;
    }

    if (this.form.invalid) {
      this.form.markAllAsTouched();
      // Send focus to the first field that needs attention.
      const firstInvalid = Object.keys(this.form.controls).find(
        (key) => this.form.get(key)?.invalid,
      );
      document.getElementById(firstInvalid ?? '')?.focus();
      return;
    }

    this.submitting.set(true);
    const raw = this.form.getRawValue();

    this.store
      .createOrder({
        customer_id: raw.customer_id.trim(),
        product_sku: raw.product_sku,
        quantity: Number(raw.quantity),
      })
      .subscribe({
        next: () => {
          this.submitting.set(false);
          // Keep the customer id: operators usually raise several orders in a row.
          this.form.patchValue({ quantity: 1 });
          this.form.controls.quantity.markAsPristine();
          this.created.emit();
        },
        error: (error) => {
          this.submitting.set(false);
          this.toasts.error('Order was not created', describeError(error));
        },
      });
  }
}
