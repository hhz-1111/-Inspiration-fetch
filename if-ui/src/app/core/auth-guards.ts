import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { map } from 'rxjs';
import { AuthService } from './auth';

export const authGuard:CanActivateFn=()=>{const auth=inject(AuthService),router=inject(Router);if(auth.user())return true;return auth.me().pipe(map(user=>user?true:router.createUrlTree(['/login'])));};
export const adminGuard:CanActivateFn=()=>{const auth=inject(AuthService),router=inject(Router);const decide=(role?:string)=>role==='admin'?true:router.createUrlTree(['/collect']);if(auth.user())return decide(auth.user()?.role);return auth.me().pipe(map(user=>decide(user?.role)));};
export const reverseAuthGuard: CanActivateFn = () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (auth.user()) return router.createUrlTree(['/collect']);
  return true;
};
