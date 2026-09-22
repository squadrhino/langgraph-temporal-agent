import { Injectable, computed, signal } from '@angular/core';

/**
 * OpenID Connect Authorization Code flow with PKCE, against Keycloak.
 *
 * Written directly rather than pulled from a library because the flow is small
 * and the dependency surface is not worth it for a lab. The security-relevant
 * parts are: a per-attempt code_verifier that never leaves this origin, a state
 * parameter checked on return, and tokens held in sessionStorage so they die
 * with the tab.
 *
 * This replaces CustomerSessionService's typed-in customer ID. The customer a
 * caller acts as now comes from the `customer_id` token claim, which only
 * Keycloak can set and which the API verifies independently — the browser is no
 * longer trusted to assert identity.
 */

const ISSUER = 'http://keycloak.agentops.local/realms/agentops';
const CLIENT_ID = 'agentops-app';
const SCOPE = 'openid profile email';

const TOKEN_KEY = 'ao-access-token';
const REFRESH_KEY = 'ao-refresh-token';
const VERIFIER_KEY = 'ao-pkce-verifier';
const STATE_KEY = 'ao-oidc-state';
const RETURN_KEY = 'ao-return-to';

export interface AuthUser {
  userId: string;
  username: string;
  customerId: string | null;
  roles: string[];
  isStaff: boolean;
  isApprover: boolean;
}

interface TokenResponse {
  access_token: string;
  refresh_token?: string;
  expires_in?: number;
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = '';
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function randomString(length = 64): string {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return base64UrlEncode(bytes);
}

async function challengeFor(verifier: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return base64UrlEncode(new Uint8Array(digest));
}

function decodeClaims(token: string): Record<string, unknown> {
  try {
    const part = token.split('.')[1];
    const padded = part.replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(padded + '='.repeat((4 - (padded.length % 4)) % 4)));
  } catch {
    return {};
  }
}

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch {
    // Blocked storage: the session still holds for this page view.
  }
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly token = signal<string | null>(read(TOKEN_KEY));
  readonly user = signal<AuthUser | null>(null);

  readonly isAuthenticated = computed(() => this.token() !== null);
  readonly roles = computed(() => this.user()?.roles ?? []);
  readonly isStaff = computed(() => this.user()?.isStaff ?? false);
  readonly isApprover = computed(() => this.user()?.isApprover ?? false);
  /** The customer this session acts as, from the token — not user input. */
  readonly customerId = computed(() => this.user()?.customerId ?? null);

  get accessToken(): string | null {
    return this.token();
  }

  /** Called once at startup: completes a redirect, or restores an existing session. */
  async initialize(): Promise<void> {
    const params = new URLSearchParams(window.location.search);
    if (params.get('code') && params.get('state')) {
      await this.completeLogin(params);
      return;
    }
    if (this.token()) {
      await this.loadUser();
    }
  }

  async login(returnTo: string = window.location.pathname): Promise<void> {
    const verifier = randomString();
    const state = randomString(16);
    write(VERIFIER_KEY, verifier);
    write(STATE_KEY, state);
    write(RETURN_KEY, returnTo);

    const query = new URLSearchParams({
      client_id: CLIENT_ID,
      redirect_uri: this.redirectUri(),
      response_type: 'code',
      scope: SCOPE,
      state,
      code_challenge: await challengeFor(verifier),
      code_challenge_method: 'S256',
    });
    window.location.assign(`${ISSUER}/protocol/openid-connect/auth?${query}`);
  }

  logout(): void {
    const refresh = read(REFRESH_KEY);
    this.clear();
    const query = new URLSearchParams({
      client_id: CLIENT_ID,
      post_logout_redirect_uri: window.location.origin + '/',
    });
    if (refresh) {
      // Best effort: tells Keycloak to drop the session too, not just this tab.
      void fetch(`${ISSUER}/protocol/openid-connect/logout`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({ client_id: CLIENT_ID, refresh_token: refresh }),
      }).catch(() => undefined);
    }
    window.location.assign(`${ISSUER}/protocol/openid-connect/logout?${query}`);
  }

  private redirectUri(): string {
    return window.location.origin + '/';
  }

  private clear(): void {
    write(TOKEN_KEY, null);
    write(REFRESH_KEY, null);
    write(VERIFIER_KEY, null);
    write(STATE_KEY, null);
    this.token.set(null);
    this.user.set(null);
  }

  private async completeLogin(params: URLSearchParams): Promise<void> {
    const expected = read(STATE_KEY);
    const verifier = read(VERIFIER_KEY);
    const returnTo = read(RETURN_KEY) || '/';

    // Strip the code from the address bar whatever happens next, so a reload
    // cannot replay it.
    window.history.replaceState({}, '', returnTo);
    write(STATE_KEY, null);
    write(VERIFIER_KEY, null);
    write(RETURN_KEY, null);

    if (!expected || params.get('state') !== expected || !verifier) {
      // Mismatched state means this redirect did not originate here.
      return;
    }

    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: CLIENT_ID,
      code: params.get('code') as string,
      redirect_uri: this.redirectUri(),
      code_verifier: verifier,
    });

    const response = await fetch(`${ISSUER}/protocol/openid-connect/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
    });
    if (!response.ok) return;

    const tokens = (await response.json()) as TokenResponse;
    write(TOKEN_KEY, tokens.access_token);
    write(REFRESH_KEY, tokens.refresh_token ?? null);
    this.token.set(tokens.access_token);
    await this.loadUser();
  }

  /**
   * Ask the API who it thinks we are.
   *
   * Deliberately not read from the token in the browser: the API is the
   * authority on roles and customer scope, and reading it back catches a token
   * the API would reject before the UI renders as though it were signed in.
   */
  private async loadUser(): Promise<void> {
    const token = this.token();
    if (!token) return;
    try {
      const r = await fetch('/me', { headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) {
        this.clear();
        return;
      }
      const me = await r.json();
      if (!me.authenticated) {
        this.clear();
        return;
      }
      this.user.set({
        userId: me.userId,
        username: me.username,
        customerId: me.customerId ?? null,
        roles: me.roles ?? [],
        isStaff: !!me.isStaff,
        isApprover: !!me.isApprover,
      });
    } catch {
      this.clear();
    }
  }

  /** Claims of the current token, for display only. */
  claims(): Record<string, unknown> {
    const t = this.token();
    return t ? decodeClaims(t) : {};
  }
}
