import { Routes } from '@angular/router';
import { adminGuard, authGuard, reverseAuthGuard } from './core/auth-guards';

export const routes:Routes=[
  {path:'login',canActivate:[reverseAuthGuard],loadComponent:()=>import('./pages/login/login').then(m=>m.LoginPage)},
  {path:'collect',canActivate:[authGuard],loadComponent:()=>import('./pages/collect/collect').then(m=>m.CollectPage)},
  {path:'analysis',canActivate:[authGuard],loadComponent:()=>import('./pages/analysis/analysis').then(m=>m.AnalysisPage)},
  {path:'inspirations',canActivate:[authGuard],loadComponent:()=>import('./pages/inspirations/inspirations').then(m=>m.InspirationsPage)},
  {path:'users',canActivate:[adminGuard],loadComponent:()=>import('./pages/users/users').then(m=>m.UsersPage)},
  {path:'',pathMatch:'full',redirectTo:'collect'},
  {path:'**',loadComponent:()=>import('./pages/not-found/not-found').then(m=>m.NotFoundPage)},
];
