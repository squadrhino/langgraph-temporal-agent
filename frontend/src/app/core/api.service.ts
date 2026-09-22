import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { Injectable, NgZone, inject } from '@angular/core';
import { Observable, map } from 'rxjs';

import { AuthService } from './auth.service';
import { AgentResponse, Health, Order, OrderCreate, OrderStatus, Product } from './models';

/**
 * The API models prices and totals as `Decimal`, which Pydantic serialises as a
 * JSON string ("1498.00"). Everything downstream does arithmetic on them, so the
 * transport layer is where they become numbers — accepting either shape keeps
 * the UI working whichever way the backend serialises them.
 */
type RawProduct = Omit<Product, 'price'> & { price: number | string };
type RawOrder = Omit<Order, 'product' | 'total'> & {
  product: RawProduct;
  total: number | string;
};

function toNumber(value: number | string): number {
  const parsed = typeof value === 'number' ? value : Number.parseFloat(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function toProduct(raw: RawProduct): Product {
  return { ...raw, price: toNumber(raw.price) };
}

function toOrder(raw: RawOrder): Order {
  return { ...raw, product: toProduct(raw.product), total: toNumber(raw.total) };
}

/** An agent reply plus the request id the API middleware stamped on the response. */
export interface AgentReply {
  message: string;
  requestId: string | null;
}

/** Marks a stream failure that the plain `POST /agent` endpoint can serve instead. */
export const STREAM_UNAVAILABLE = 'AgentStreamUnavailable';

/** What `POST /agent/chat/stream` emits, frame by frame. */
export type AgentStreamEvent =
  | { type: 'open'; requestId: string | null }
  | { type: 'chunk'; text: string };

/**
 * Parse one SSE frame — an `event:` line plus one or more `data:` lines.
 *
 * Exported for tests: frame parsing is the part of streaming most likely to
 * break, and it is pure.
 */
export function parseSseFrame(frame: string): { event: string; data: string } | null {
  let event = 'message';
  const data: string[] = [];

  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) {
      continue;
    }
    const separator = line.indexOf(':');
    const field = separator === -1 ? line : line.slice(0, separator);
    const value = separator === -1 ? '' : line.slice(separator + 1).trimStart();

    if (field === 'event') {
      event = value;
    } else if (field === 'data') {
      data.push(value);
    }
  }

  return data.length || event !== 'message' ? { event, data: data.join('\n') } : null;
}

/**
 * Thin transport layer over the Computer Store API.
 * Paths stay relative so the same build works behind `ng serve` (proxied)
 * and when FastAPI serves the compiled bundle itself.
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);
  private readonly zone = inject(NgZone);
  private readonly auth = inject(AuthService);

  health(): Observable<Health> {
    return this.http.get<Health>('/health');
  }

  products(): Observable<Product[]> {
    return this.http.get<RawProduct[]>('/products').pipe(map((rows) => rows.map(toProduct)));
  }

  orders(options: { status?: OrderStatus | ''; limit?: number } = {}): Observable<Order[]> {
    let params = new HttpParams();
    if (options.status) {
      params = params.set('status', options.status);
    }
    if (options.limit) {
      params = params.set('limit', options.limit);
    }
    return this.http.get<RawOrder[]>('/orders', { params }).pipe(map((rows) => rows.map(toOrder)));
  }

  createOrder(body: OrderCreate): Observable<Order> {
    return this.http.post<RawOrder>('/orders', body).pipe(map(toOrder));
  }

  /**
   * Stream a reply from the agent.
   *
   * HttpClient buffers response bodies, so streaming goes through fetch and a
   * ReadableStream reader. Emissions are pushed back inside the Angular zone so
   * change detection still runs for them.
   */
  streamAgent(message: string, threadId: string, userId: string): Observable<AgentStreamEvent> {
    return new Observable<AgentStreamEvent>((subscriber) => {
      const controller = new AbortController();
      const emit = (event: AgentStreamEvent) => this.zone.run(() => subscriber.next(event));

      const run = async (): Promise<void> => {
        // This request goes through fetch() rather than HttpClient, so the
        // HTTP interceptor does not apply and the token must be attached here.
        // Without it the API sees an anonymous caller.
        const token = this.auth.accessToken;
        const response = await fetch('/agent/chat/stream', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          },
          body: JSON.stringify({ message, thread_id: threadId, user_id: userId }),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          const failure = new Error(await describeStreamFailure(response));
          // A server without the streaming route: callers can retry the
          // non-streaming endpoint instead of showing an error.
          if (response.status === 404) {
            failure.name = STREAM_UNAVAILABLE;
          }
          throw failure;
        }

        emit({ type: 'open', requestId: response.headers.get('X-Request-ID') });

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        for (;;) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          // Frames are separated by a blank line; keep any partial tail.
          const frames = buffer.split(/\r?\n\r?\n/);
          buffer = frames.pop() ?? '';

          for (const raw of frames) {
            const frame = parseSseFrame(raw);
            if (!frame) {
              continue;
            }
            if (frame.event === 'done') {
              return;
            }
            if (frame.event === 'error') {
              throw new Error(readContent(frame.data) || 'The agent stream failed.');
            }
            const text = readContent(frame.data);
            if (text) {
              emit({ type: 'chunk', text });
            }
          }
        }
      };

      run()
        .then(() => this.zone.run(() => subscriber.complete()))
        .catch((error: unknown) => {
          if (controller.signal.aborted) {
            return;
          }
          this.zone.run(() => subscriber.error(error));
        });

      return () => controller.abort();
    });
  }

  askAgent(message: string): Observable<AgentReply> {
    return this.http
      .post<AgentResponse>('/agent', { message }, { observe: 'response' })
      .pipe(
        map((response) => ({
          message: response.body?.message ?? '',
          requestId: response.headers.get('X-Request-ID'),
        })),
      );
  }
}

/** Pull the text out of an SSE `data:` payload, JSON or plain. */
function readContent(data: string): string {
  if (!data) {
    return '';
  }
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed === 'string') {
      return parsed;
    }
    if (parsed && typeof parsed === 'object') {
      const record = parsed as Record<string, unknown>;
      const text = record['content'] ?? record['message'] ?? record['text'] ?? record['detail'];
      return typeof text === 'string' ? text : '';
    }
    return '';
  } catch {
    return data;
  }
}

async function describeStreamFailure(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === 'string') {
      return detail;
    }
  } catch {
    // Not JSON; fall through to the status line.
  }
  return `${response.status} ${response.statusText || 'Stream failed'}`;
}

/** Turn an HTTP failure into something a human can act on. */
export function describeError(error: unknown): string {
  if (error instanceof Error && !(error instanceof HttpErrorResponse)) {
    return error.message === 'Failed to fetch'
      ? 'Cannot reach the API. Is uvicorn running on port 8000?'
      : error.message;
  }

  if (!(error instanceof HttpErrorResponse)) {
    return 'Unexpected error. Check the browser console for details.';
  }

  if (error.status === 0) {
    return 'Cannot reach the API. Is uvicorn running on port 8000?';
  }

  const detail = (error.error as { detail?: unknown } | null)?.detail;

  if (typeof detail === 'string') {
    return detail;
  }

  // FastAPI validation errors: [{ loc: [...], msg: '...' }]
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item: { loc?: unknown[]; msg?: string }) => {
        const field = Array.isArray(item.loc) ? item.loc[item.loc.length - 1] : undefined;
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean);
    if (messages.length) {
      return messages.join(' · ');
    }
  }

  return `${error.status} ${error.statusText || 'Request failed'}`;
}
