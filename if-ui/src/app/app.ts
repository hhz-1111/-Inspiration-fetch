import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { AuthService } from './core/auth';
import { FetchApi } from './core/fetch-api';

@Component({selector:'app-root',imports:[RouterLink,RouterLinkActive,RouterOutlet,FormsModule],templateUrl:'./app.html',styleUrl:'./app.scss'})
export class App{
  readonly auth=inject(AuthService);
  private readonly router=inject(Router);
  private readonly api=inject(FetchApi);
  readonly settingsOpen=signal(false);
  readonly apiKeyDraft=signal('');
  readonly apiConfigured=signal(false);
  readonly apiKeySource=signal('');
  readonly saving=signal(false);
  readonly saveMsg=signal('');

  logout(){this.auth.logout().subscribe(()=>this.router.navigateByUrl('/login'));}

  openSettings(){
    this.saveMsg.set('');
    this.api.getSettings().subscribe({
      next: r => {
        // 仅用户自己保存过的 key 回显到编辑框；全局 key（config/env）只显示"已配置"
        this.apiKeyDraft.set(r.source==='user' ? (r.mimo_api_key||'') : '');
        this.apiConfigured.set(!!r.configured);
        this.apiKeySource.set(r.source||'');
        this.settingsOpen.set(true);
      },
      error: () => { this.apiKeyDraft.set(''); this.apiConfigured.set(false); this.apiKeySource.set(''); this.settingsOpen.set(true); },
    });
  }

  closeSettings(){this.settingsOpen.set(false);this.saveMsg.set('');}

  saveSettings(){
    this.saving.set(true); this.saveMsg.set('');
    const key = this.apiKeyDraft().trim();
    this.api.updateSettings(key).subscribe({
      next: () => { this.apiConfigured.set(!!key || this.apiKeySource()!==''); this.saveMsg.set(key?'已保存到当前账号':'已清空当前账号配置'); this.saving.set(false); },
      error: () => { this.saveMsg.set('保存失败'); this.saving.set(false); },
    });
  }
}
