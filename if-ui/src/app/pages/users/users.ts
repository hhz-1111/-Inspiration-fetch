import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { AuthService, ManagedUser } from '../../core/auth';

@Component({selector:'app-users',templateUrl:'./users.html',styleUrl:'./users.scss',imports:[FormsModule]})
export class UsersPage{
  private readonly auth=inject(AuthService);readonly users=signal<ManagedUser[]>([]);readonly loading=signal(true);readonly message=signal('');
  readonly resetTarget = signal<ManagedUser | null>(null);
  readonly resetPassword = signal('');
  readonly resetError = signal('');
  constructor(){this.load();}
  load(){this.auth.users().subscribe({next:r=>{this.users.set(r.users);this.loading.set(false)},error:e=>{this.message.set(this.auth.error(e));this.loading.set(false)}});}
  approve(user:ManagedUser){this.auth.approve(user.id).subscribe(()=>this.load());}
  reject(user:ManagedUser){this.auth.reject(user.id).subscribe(()=>this.load());}
  openReset(user:ManagedUser){this.resetTarget.set(user);this.resetPassword.set('');this.resetError.set('');}
  closeReset(){this.resetTarget.set(null);this.resetPassword.set('');this.resetError.set('');}
  confirmReset(){
    const user=this.resetTarget();if(!user)return;
    const pw=this.resetPassword().trim();
    if(pw.length<8){this.resetError.set('密码至少需要 8 位');return;}
    this.auth.resetPassword(user.id,pw).subscribe({next:()=>{this.message.set('密码已重置');this.closeReset();},error:e=>{this.resetError.set(this.auth.error(e));}});
  }
  remove(user:ManagedUser){if(!window.confirm(`删除 ${user.username} 及其全部采集、授权和待分析数据？`))return;this.auth.deleteUser(user.id).subscribe(()=>this.load());}
  date(value:string){return new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));}
}
