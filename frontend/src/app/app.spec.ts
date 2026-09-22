import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';

import { App } from './app';
import { routes } from './app.routes';
import { CustomerPortalComponent } from './pages/customer-portal.component';
import { CustomerSessionService } from './core/customer-session.service';

describe('App', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter(routes)],
    }).compileComponents();
    TestBed.inject(CustomerSessionService).signOut();
  });

  it('creates the shell', () => {
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    expect(fixture.componentInstance).toBeTruthy();
  });

  it('routes / to the operations console', async () => {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('h1')?.textContent).toContain(
      'Computer Store',
    );
    expect(harness.routeNativeElement?.querySelector('#orders-title')).toBeTruthy();
  });

  it('routes /customer to the sign-in form', async () => {
    const harness = await RouterTestingHarness.create();
    await harness.navigateByUrl('/customer');
    harness.detectChanges();
    expect(harness.routeNativeElement?.querySelector('#customer-id')).toBeTruthy();
    expect(harness.routeNativeElement?.querySelector('app-agent-chat')).toBeNull();
  });
});

describe('CustomerPortalComponent', () => {
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [CustomerPortalComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter(routes)],
    }).compileComponents();
    TestBed.inject(CustomerSessionService).signOut();
  });

  it('rejects an id that is not in the CUS-000 form', () => {
    const fixture = TestBed.createComponent(CustomerPortalComponent);
    fixture.detectChanges();
    const input = fixture.nativeElement.querySelector('#customer-id') as HTMLInputElement;
    input.value = 'nope';
    input.dispatchEvent(new Event('input'));
    fixture.nativeElement.querySelector('button[type=submit]').click();
    fixture.detectChanges();

    expect(TestBed.inject(CustomerSessionService).customerId()).toBeNull();
    expect(fixture.nativeElement.querySelector('#customer-id-error')).toBeTruthy();
  });

  it('normalises a valid id and swaps in the chat', () => {
    const fixture = TestBed.createComponent(CustomerPortalComponent);
    fixture.detectChanges();
    const input = fixture.nativeElement.querySelector('#customer-id') as HTMLInputElement;
    input.value = 'cus-028';
    input.dispatchEvent(new Event('input'));
    fixture.nativeElement.querySelector('button[type=submit]').click();
    fixture.detectChanges();

    expect(TestBed.inject(CustomerSessionService).customerId()).toBe('CUS-028');
    expect(fixture.nativeElement.querySelector('app-agent-chat')).toBeTruthy();
  });
});
