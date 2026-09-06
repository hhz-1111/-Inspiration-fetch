import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, throwError } from 'rxjs';
import { AuthService } from './auth';

export const authInterceptor: HttpInterceptorFn = (request, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  return next(request).pipe(catchError((error: HttpErrorResponse) => {
    if (error.status === 401 && !request.url.includes('/auth/login') && !request.url.includes('/auth/me')) {
      auth.user.set(null);
      router.navigateByUrl('/login');
    }
    return throwError(() => error);
  }));
};
