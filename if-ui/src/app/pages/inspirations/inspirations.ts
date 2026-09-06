import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { Subject, Subscription, timer } from 'rxjs';
import { switchMap, takeUntil } from 'rxjs/operators';
import { FetchApi, InspirationAnalysis } from '../../core/fetch-api';

@Component({ selector: 'app-inspirations', imports: [RouterLink], templateUrl: './inspirations.html', styleUrl: './inspirations.scss' })
export class InspirationsPage {
  private readonly api = inject(FetchApi);
  private readonly destroyRef = inject(DestroyRef);
  readonly items = signal<InspirationAnalysis[]>([]);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly activeKeyword = signal('全部');
  readonly playbackUrl = signal('');
  readonly playbackLoading = signal(false);
  readonly playbackError = signal('');
  readonly deletingId = signal<number | null>(null);
  readonly selectedItem = signal<InspirationAnalysis | null>(null);
  private pollSub?: Subscription;
  private destroy$ = new Subject<void>();

  readonly keywords = computed(() => ['全部', ...new Set(this.items().map(item => item.keyword))]);
  readonly visibleItems = computed(() => this.activeKeyword() === '全部' ? this.items() : this.items().filter(item => item.keyword === this.activeKeyword()));
  readonly completedCount = computed(() => this.items().filter(item => item.analysis_status === 'completed').length);

  constructor() {
    this.load();
    this.destroyRef.onDestroy(() => { this.destroy$.next(); this.destroy$.complete(); this.pollSub?.unsubscribe(); });
  }

  load(): void {
    this.loading.set(true); this.error.set('');
    this.api.inspirations().subscribe({
      next: response => { this.items.set(response.items); this.loading.set(false); this.updatePolling(); },
      error: () => { this.error.set('无法读取灵感报告，请确认后端服务已启动'); this.loading.set(false); },
    });
  }

  private refresh(): void {
    this.api.inspirations().subscribe({
      next: response => {
        this.items.set(response.items);
        const selected = this.selectedItem();
        if (selected) this.selectedItem.set(response.items.find(item => item.id === selected.id) ?? null);
        this.updatePolling();
      },
      error: () => { this.error.set('自动刷新失败，将在下次轮询重试'); },
    });
  }

  private updatePolling(): void {
    const hasPending = this.items().some(item => item.analysis_status === 'pending' || item.analysis_status === 'processing');
    if (hasPending && !this.pollSub) {
      this.pollSub = timer(30_000, 30_000).pipe(
        switchMap(() => this.api.inspirations()),
        takeUntil(this.destroy$),
      ).subscribe({
        next: response => {
          this.items.set(response.items);
          const selected = this.selectedItem();
          if (selected) this.selectedItem.set(response.items.find(item => item.id === selected.id) ?? null);
          const stillPending = response.items.some(item => item.analysis_status === 'pending' || item.analysis_status === 'processing');
          if (!stillPending) this.stopPolling();
        },
        error: () => { this.error.set('自动刷新失败，将在下次轮询重试'); },
      });
    } else if (!hasPending) {
      this.stopPolling();
    }
  }

  private stopPolling(): void { this.pollSub?.unsubscribe(); this.pollSub = undefined; }

  play(video: InspirationAnalysis): void {
    this.playbackUrl.set(''); this.playbackError.set(''); this.playbackLoading.set(true);
    this.api.playback(video.id).subscribe({
      next: response => { this.playbackUrl.set(response.media_url); this.playbackLoading.set(false); },
      error: () => { this.playbackError.set('无法取得播放地址，请检查平台登录状态'); this.playbackLoading.set(false); },
    });
  }

  closePlayer(): void { this.playbackUrl.set(''); this.playbackError.set(''); this.playbackLoading.set(false); }
  openItem(item: InspirationAnalysis): void { this.selectedItem.set(item); }
  closeItem(): void { this.selectedItem.set(null); }
  abandon(item: InspirationAnalysis): void {
    if (!window.confirm(`确定放弃“${item.title}”吗？视频及其分析数据将被永久删除，且无法恢复。`)) return;
    this.deletingId.set(item.id); this.error.set('');
    this.api.deleteVideo(item.id).subscribe({
      next: () => { this.items.update(items => items.filter(current => current.id !== item.id)); this.selectedItem.set(null); this.deletingId.set(null); },
      error: () => { this.error.set('放弃灵感失败，请稍后重试'); this.deletingId.set(null); },
    });
  }
  formatDate(value?: string): string { return value ? new Intl.DateTimeFormat('zh-CN', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' }).format(new Date(value)) : '--'; }
  statusLabel(status: InspirationAnalysis['analysis_status']): string { return ({ pending:'等待分析', processing:'分析中', completed:'分析完成', failed:'分析失败' })[status]; }
}
