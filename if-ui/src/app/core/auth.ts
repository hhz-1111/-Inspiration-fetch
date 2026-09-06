import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, catchError, map, of, tap } from 'rxjs';

export interface AppUser { id:number; username:string; role:'admin'|'user'; status:string; created_at:string; approved_at?:string; }
export interface ManagedUser extends AppUser {}

@Injectable({ providedIn:'root' })
export class AuthService {
  private readonly http=inject(HttpClient); private readonly base='/api';
  readonly user=signal<AppUser|null>(null);
  me():Observable<AppUser|null>{return this.http.get<{user:AppUser}>(`${this.base}/auth/me`).pipe(map(r=>r.user),tap(user=>this.user.set(user)),catchError(()=>{this.user.set(null);return of(null)}));}
  login(username:string,password:string){return this.http.post<{user:AppUser}>(`${this.base}/auth/login`,{username,password}).pipe(tap(r=>this.user.set(r.user)));}
  register(username:string,password:string){return this.http.post<{message:string}>(`${this.base}/auth/register`,{username,password});}
  logout(){return this.http.post<void>(`${this.base}/auth/logout`,{}).pipe(tap(()=>this.user.set(null)));}
  users(){return this.http.get<{users:ManagedUser[]}>(`${this.base}/admin/users`);}
  approve(id:number){return this.http.patch(`${this.base}/admin/users/${id}/approve`,{});}
  reject(id:number){return this.http.patch(`${this.base}/admin/users/${id}/reject`,{});}
  resetPassword(id:number,password:string){return this.http.patch(`${this.base}/admin/users/${id}/password`,{password});}
  deleteUser(id:number){return this.http.delete<void>(`${this.base}/admin/users/${id}`);}
  error(error:unknown):string{return error instanceof HttpErrorResponse?(error.error?.detail||'请求失败'):'请求失败';}
}
