import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

@Component({
  selector: 'app-not-found',
  imports: [RouterLink],
  template: `
    <section class="not-found">
      <span class="code">404</span>
      <h2>页面不存在</h2>
      <p>你访问的页面不存在或已被移除。</p>
      <a routerLink="/collect" class="primary">返回首页</a>
    </section>
  `,
  styles: `
    .not-found { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh; text-align: center; }
    .code { font-size: 4rem; font-weight: 700; color: var(--accent); margin-bottom: 0.5rem; }
    h2 { font-size: 1.5rem; margin-bottom: 0.5rem; }
    p { color: var(--muted); margin-bottom: 1.5rem; }
    .primary { padding: 0.6rem 1.5rem; background: var(--accent); color: #fff; border-radius: 6px; text-decoration: none; }
  `,
})
export class NotFoundPage {}
