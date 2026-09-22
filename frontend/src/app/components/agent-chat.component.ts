import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';

import { ApiService, STREAM_UNAVAILABLE, describeError } from '../core/api.service';
import { IconComponent } from '../shared/icon.component';

export interface ChatMessage {
  id: number;
  role: 'user' | 'agent' | 'error';
  text: string;
  at: Date;
  requestId?: string | null;
  /** True while chunks are still arriving for this message. */
  streaming?: boolean;
}

const MAX_LENGTH = 2000;

const DEFAULT_SUGGESTIONS = [
  'Where is order ORD-004?',
  'ORD-012 arrived damaged — am I eligible for a refund?',
  'Which laptops do you sell?',
];

/**
 * Thin client for `POST /agent`, shared by the staff console and the customer
 * portal. The endpoint is still a stub, so the panel is built to make that
 * obvious rather than to fake an assistant.
 */
@Component({
  selector: 'app-agent-chat',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [IconComponent],
  templateUrl: './agent-chat.component.html',
  styleUrl: './agent-chat.component.scss',
})
export class AgentChatComponent {
  private readonly api = inject(ApiService);
  private readonly log = viewChild<ElementRef<HTMLDivElement>>('log');

  /** Heading shown in the card. */
  readonly heading = input('Support agent');
  /** Staff see the stub tag and the X-Request-ID; customers should not. */
  readonly showDiagnostics = input(true);
  readonly suggestions = input<readonly string[]>(DEFAULT_SUGGESTIONS);
  readonly placeholder = input('Ask about an order, a refund or a product…');
  readonly introTitle = input('Ask the support agent');
  readonly introBody = input('');
  readonly userId = input('STAFF');

  protected readonly maxLength = MAX_LENGTH;
  protected readonly messages = signal<ChatMessage[]>([]);
  protected readonly draft = signal('');
  protected readonly pending = signal(false);
  private readonly threadId = crypto.randomUUID();
  private nextId = 1;

  protected onInput(event: Event): void {
    this.draft.set((event.target as HTMLTextAreaElement).value);
  }

  /** Enter sends, Shift+Enter inserts a newline. */
  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      this.send();
    }
  }

  protected use(suggestion: string): void {
    this.draft.set(suggestion);
    this.send();
  }

  protected send(): void {
    const text = this.draft().trim();
    if (!text || this.pending()) {
      return;
    }

    this.append({ role: 'user', text });
    this.draft.set('');
    this.pending.set(true);

    // The reply lands in one bubble that grows as chunks arrive.
    const replyId = this.append({ role: 'agent', text: '', streaming: true });

    this.api.streamAgent(text, this.threadId, this.userId()).subscribe({
      next: (event) => {
        if (event.type === 'open') {
          this.patch(replyId, (message) => ({ ...message, requestId: event.requestId }));
          return;
        }
        this.patch(replyId, (message) => ({ ...message, text: message.text + event.text }));
        this.scrollToLatest();
      },
      error: (error) => {
        if (error instanceof Error && error.name === STREAM_UNAVAILABLE) {
          this.fallback(text, replyId);
          return;
        }

        this.pending.set(false);
        // Keep whatever streamed through, and say what went wrong after it.
        this.patch(replyId, (message) =>
          message.text
            ? { ...message, streaming: false }
            : { ...message, role: 'error', text: describeError(error), streaming: false },
        );
        if (this.messages().find((message) => message.id === replyId)?.role === 'agent') {
          this.append({ role: 'error', text: describeError(error) });
        }
        this.scrollToLatest();
      },
      complete: () => {
        this.pending.set(false);
        this.patch(replyId, (message) => ({
          ...message,
          streaming: false,
          text: message.text || 'The agent returned an empty reply.',
        }));
        this.scrollToLatest();
      },
    });
  }

  /** Older servers only expose POST /agent; answer from there in one shot. */
  private fallback(text: string, replyId: number): void {
    this.api.askAgent(text).subscribe({
      next: (reply) => {
        this.pending.set(false);
        this.patch(replyId, (message) => ({
          ...message,
          text: reply.message,
          requestId: reply.requestId,
          streaming: false,
        }));
        this.scrollToLatest();
      },
      error: (error) => {
        this.pending.set(false);
        this.patch(replyId, (message) => ({
          ...message,
          role: 'error',
          text: describeError(error),
          streaming: false,
        }));
        this.scrollToLatest();
      },
    });
  }

  private patch(id: number, update: (message: ChatMessage) => ChatMessage): void {
    this.messages.update((all) =>
      all.map((message) => (message.id === id ? update(message) : message)),
    );
  }

  private append(
    message: Pick<ChatMessage, 'role' | 'text'> & { requestId?: string | null; streaming?: boolean },
  ): number {
    const id = this.nextId++;
    this.messages.update((all) => [...all, { id, at: new Date(), ...message }]);
    this.scrollToLatest();
    return id;
  }

  /** A microtask runs before the new content is rendered, so wait a task. */
  private scrollToLatest(): void {
    setTimeout(() => {
      const element = this.log()?.nativeElement;
      if (element) {
        element.scrollTop = element.scrollHeight;
      }
    });
  }
}
