import { Injectable, signal } from '@angular/core';

export type ToastKind = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  title: string;
  body?: string;
}

const DISMISS_AFTER_MS = 5000;

@Injectable({ providedIn: 'root' })
export class ToastService {
  private nextId = 1;
  readonly toasts = signal<Toast[]>([]);

  success(title: string, body?: string): void {
    this.push('success', title, body);
  }

  error(title: string, body?: string): void {
    this.push('error', title, body);
  }

  info(title: string, body?: string): void {
    this.push('info', title, body);
  }

  dismiss(id: number): void {
    this.toasts.update((all) => all.filter((toast) => toast.id !== id));
  }

  private push(kind: ToastKind, title: string, body?: string): void {
    const id = this.nextId++;
    this.toasts.update((all) => [...all, { id, kind, title, body }]);
    setTimeout(() => this.dismiss(id), DISMISS_AFTER_MS);
  }
}
