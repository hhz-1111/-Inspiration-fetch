import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AuthService } from '../../core/auth';

@Component({selector:'app-login',imports:[FormsModule],templateUrl:'./login.html',styleUrl:'./login.scss'})
export class LoginPage{
  private readonly auth=inject(AuthService);private readonly router=inject(Router);
  mode=signal<'login'|'register'>('login');username='';password='';loading=signal(false);message=signal('');error=signal('');
  submit(){this.loading.set(true);this.error.set('');this.message.set('');if(this.mode()==='login'){this.auth.login(this.username,this.password).subscribe({next:()=>{this.loading.set(false);this.router.navigateByUrl('/collect')},error:(e:unknown)=>{this.loading.set(false);this.error.set(this.auth.error(e))}});}else{this.auth.register(this.username,this.password).subscribe({next:()=>{this.loading.set(false);this.message.set('注册成功，请等待管理员审批后登录');this.mode.set('login');this.password='';},error:(e:unknown)=>{this.loading.set(false);this.error.set(this.auth.error(e))}});}}
  switchMode(mode:'login'|'register'){this.mode.set(mode);this.error.set('');this.message.set('');}
}
