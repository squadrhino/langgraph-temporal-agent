import { Routes } from '@angular/router';

export const routes: Routes = [
  {
    path: '',
    title: 'Computer Store — Operations Console',
    loadComponent: () =>
      import('./pages/operations-console.component').then((m) => m.OperationsConsoleComponent),
  },
  {
    path: 'customer',
    title: 'Computer Store — Customer Support',
    loadComponent: () =>
      import('./pages/customer-portal.component').then((m) => m.CustomerPortalComponent),
  },
  { path: '**', redirectTo: '' },
];
