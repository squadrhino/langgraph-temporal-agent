import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

import { AuthService } from './auth.service';

/**
 * Attaches the access token to same-origin API calls.
 *
 * Only relative URLs are touched: an absolute URL could be a third party, and
 * sending a bearer token there would leak it.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const token = auth.accessToken;

  const sameOrigin = !/^https?:\/\//i.test(req.url) || req.url.startsWith(window.location.origin);
  if (!token || !sameOrigin) {
    return next(req);
  }

  return next(req.clone({ setHeaders: { Authorization: `Bearer ${token}` } }));
};
