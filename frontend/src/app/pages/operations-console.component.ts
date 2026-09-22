import { ChangeDetectionStrategy, Component, OnInit, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AgentChatComponent } from '../components/agent-chat.component';
import { AppHeaderComponent } from '../components/app-header.component';
import { KpiCardsComponent } from '../components/kpi-cards.component';
import { OrderFormComponent } from '../components/order-form.component';
import { OrdersTableComponent } from '../components/orders-table.component';
import { StatusDistributionComponent } from '../components/status-distribution.component';
import { StoreService } from '../core/store.service';
import { IconComponent } from '../shared/icon.component';

/** Staff-facing view: the whole order book, catalogue and support agent. */
@Component({
  selector: 'app-operations-console',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterLink,
    AppHeaderComponent,
    KpiCardsComponent,
    StatusDistributionComponent,
    OrdersTableComponent,
    OrderFormComponent,
    AgentChatComponent,
    IconComponent,
  ],
  templateUrl: './operations-console.component.html',
  styleUrl: './operations-console.component.scss',
})
export class OperationsConsoleComponent implements OnInit {
  private readonly store = inject(StoreService);

  ngOnInit(): void {
    this.store.init();
  }
}
