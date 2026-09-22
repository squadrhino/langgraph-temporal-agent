import { Injectable, signal } from '@angular/core';

const STORAGE_KEY = 'cs-customer-id';

/** Shape of the seeded identifiers, e.g. CUS-028. */
export const CUSTOMER_ID_PATTERN = /^CUS-\d{1,6}$/i;

export function normalizeCustomerId(raw: string): string {
  return raw.trim().toUpperCase();
}

/**
 * Holds who the customer portal is acting as.
 *
 * This is deliberately NOT authentication: the API has no accounts, sessions or
 * tokens yet, so the portal takes an identifier at face value. It lives in
 * sessionStorage so a refresh keeps the tab's identity without persisting it
 * across browser sessions.
 */
@Injectable({ providedIn: 'root' })
export class CustomerSessionService {
  readonly customerId = signal<string | null>(readStored());

  signIn(rawId: string): void {
    const customerId = normalizeCustomerId(rawId);
    this.customerId.set(customerId);
    try {
      sessionStorage.setItem(STORAGE_KEY, customerId);
    } catch {
      // Blocked storage: the identity still holds for this page view.
    }
  }

  signOut(): void {
    this.customerId.set(null);
    try {
      sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // Nothing to clean up.
    }
  }
}

function readStored(): string | null {
  try {
    const stored = sessionStorage.getItem(STORAGE_KEY);
    return stored && CUSTOMER_ID_PATTERN.test(stored) ? stored : null;
  } catch {
    return null;
  }
}
