import { Component, DestroyRef, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { Subject, Subscription, timer } from 'rxjs';
import { switchMap, takeUntil } from 'rxjs/operators';
import { FetchApi, PlatformKey, AuthState, CrawlTask, CrawlHistoryItem } from '../../core/fetch-api';
import { VideoItem, VideoStore } from '../../core/video-store';

@Component({ selector: 'app-collect', imports: [FormsModule], templateUrl: './collect.html', styleUrl: './collect.scss' })
export class CollectPage {
  readonly store = inject(VideoStore);
  private readonly router = inject(Router);
  private readonly api = inject(FetchApi);
  private readonly destroyRef = inject(DestroyRef);

  platform: PlatformKey = 'douyin';
  count = 8;
  createdTime = 'any';
  minLikes = 0;
  speedIndex = 1;
  speedOptions = [
    { value: 'sloth', label: '树懒', detail: '10 秒 / 条' },
    { value: 'human', label: '真人', detail: '5 秒 / 条' },
    { value: 'default', label: '默认', detail: '1 秒 / 条' },
    { value: 'flash', label: '闪电侠', detail: '无额外等待' },
  ];
  keywordInput = '';
  private readonly STORAGE_KEY = 'if_recent_keywords';
  readonly loading = signal(false);
  readonly message = signal('');
  readonly progress = signal(0);
  readonly history = signal<CrawlHistoryItem[]>([]);
  readonly authState = signal<AuthState>('checking');
  readonly authMessage = signal('正在检查平台登录状态');
  readonly authSessionId = signal<string | null>(null);
  readonly qrVersion = signal(0);
  readonly playingVideo = signal<VideoItem | null>(null);
  readonly playbackUrl = signal('');
  readonly playbackLoading = signal(false);
  readonly playbackError = signal('');
  private destroy$ = new Subject<void>();
  private authSub?: Subscription;
  private taskSub?: Subscription;

  constructor() {
    this.loadSavedKeywords();
    this.loadHistory();
    this.destroyRef.onDestroy(() => { this.destroy$.next(); this.destroy$.complete(); this.clearTimers(); });
    this.api.publicConfig().subscribe({ next: config => {
      if (!config.crawl_speeds.length) return;
      this.speedOptions = config.crawl_speeds.map(option => ({
        value: option.value,
        label: option.label,
        detail: option.delay_seconds ? `${option.delay_seconds} 秒 / 条` : '无额外等待',
      }));
      const preferredSpeed = this.platform === 'douyin' ? 'human' : 'default';
      const preferredIndex = this.speedOptions.findIndex(option => option.value === preferredSpeed);
      this.speedIndex = preferredIndex >= 0 ? preferredIndex : 0;
    } });
    this.checkAuth();
  }

  speedProgress(): number { return this.speedOptions.length > 1 ? this.speedIndex / (this.speedOptions.length - 1) * 100 : 0; }

  loadHistory(): void {
    this.api.crawlHistory().subscribe({
      next: result => this.history.set(result.tasks),
      error: () => this.history.set([]),
    });
  }

  cancelTask(taskId: string): void {
    this.api.cancelCrawl(taskId).subscribe({
      next: result => { this.message.set(result.message); this.loadHistory(); },
      error: error => this.message.set(this.errorMessage(error)),
    });
  }

  platformLabel(key: string): string {
    return ({ douyin: '抖音', xiaohongshu: '小红书', upload: '本地上传' } as Record<string, string>)[key] ?? key;
  }

  statusLabel(status: string): string {
    return ({ pending: '排队中', running: '采集中', completed: '已完成', failed: '失败', cancelled: '已取消' } as Record<string, string>)[status] ?? status;
  }

  platformChanged(): void {
    this.clearAuthTimer();
    this.authSessionId.set(null);
    const preferredSpeed = this.platform === 'douyin' ? 'human' : 'default';
    const preferredIndex = this.speedOptions.findIndex(option => option.value === preferredSpeed);
    this.speedIndex = preferredIndex >= 0 ? preferredIndex : 0;
    this.checkAuth();
  }

  checkAuth(): void {
    this.authState.set('checking');
    this.authMessage.set('正在验证已保存的登录状态');
    this.api.authStatus(this.platform).subscribe({
      next: status => this.applyAuthStatus(status),
      error: error => this.authFailed(error),
    });
  }

  startAuth(): void {
    this.authState.set('checking');
    this.authMessage.set('正在生成登录二维码');
    this.api.startAuth(this.platform).subscribe({
      next: status => {
        this.applyAuthStatus(status);
        if (status.session_id) {
          this.authSessionId.set(status.session_id);
          this.pollAuth(status.session_id);
        }
      },
      error: error => this.authFailed(error),
    });
  }

  logout(): void {
    this.api.logout(this.platform).subscribe({ next: () => this.checkAuth(), error: error => this.authFailed(error) });
  }

  submit(): void {
    if (this.authState() !== 'authenticated') {
      this.message.set('请先完成平台扫码登录');
      return;
    }
    const keywords = this.parseKeywords(this.keywordInput);
    if (!keywords.length) {
      this.message.set('请至少输入一个关键词');
      return;
    }
    this.loading.set(true);
    this.progress.set(0);
    this.message.set('正在创建采集任务…');
    this.saveRecentKeywords(this.keywordInput);
    this.api.startCrawl(this.platform, keywords, this.count, this.createdTime, this.minLikes, this.speedOptions[this.speedIndex].value).subscribe({
      next: task => { this.loadHistory(); this.pollTask(task.id); },
      error: error => this.taskFailed(error),
    });
  }

  /** Split typed keywords: commas (full/half), 、, ; and newlines. */
  private parseKeywords(raw: string): string[] {
    return raw
      .split(/[,，、;；\n\r]+/)
      .map(k => k.trim())
      .filter(k => k.length > 0 && k.length <= 60);
  }

  analyze(): void {
    const selectedIds = this.store.selectedVideos().map(video => video.id);
    if (!selectedIds.length) { this.message.set('请先选择至少一条视频'); return; }
    this.message.set('正在保存待分析视频…');
    this.api.enqueueAnalysis(selectedIds).subscribe({
      next: result => {
        this.message.set(result.added ? `已加入 ${result.added} 条待分析视频` : '所选视频已在待分析库中');
        this.router.navigateByUrl('/analysis');
      },
      error: error => this.message.set(this.errorMessage(error)),
    });
  }

  qrUrl(): string {
    const sessionId = this.authSessionId();
    return sessionId ? this.api.qrUrl(sessionId, this.qrVersion()) : '';
  }

  play(event: Event, video: VideoItem): void {
    event.stopPropagation(); this.playingVideo.set(video); this.playbackError.set('');
    this.playbackUrl.set(''); this.playbackLoading.set(true);
    this.api.playback(video.id).subscribe({next:r=>{video.mediaUrl=r.media_url;this.playbackUrl.set(r.media_url);this.playbackLoading.set(false);},error:e=>{this.playbackError.set(this.errorMessage(e));this.playbackLoading.set(false);}});
  }
  closePlayer(): void { this.playingVideo.set(null); this.playbackUrl.set(''); this.playbackError.set(''); }

  private pollAuth(sessionId: string): void {
    this.clearAuthTimer();
    this.authSub = timer(0, 2000).pipe(
      switchMap(() => this.api.sessionStatus(sessionId)),
      takeUntil(this.destroy$),
    ).subscribe({
      next: status => {
        this.applyAuthStatus(status);
        if (status.status === 'authenticated' || status.status === 'expired' || status.status === 'cancelled' || status.status === 'failed') {
          this.clearAuthTimer();
        } else {
          this.qrVersion.update(value => value + 1);
        }
      },
      error: error => this.authFailed(error),
    });
  }

  private pollTask(taskId: string): void {
    this.clearTaskTimer();
    this.taskSub = timer(0, 1500).pipe(
      switchMap(() => this.api.crawlStatus(taskId)),
      takeUntil(this.destroy$),
    ).subscribe({
      next: task => {
        this.progress.set(task.progress);
        this.message.set(task.message);
        if (task.status === 'completed') {
          this.finishTask(task);
        } else if (task.status === 'failed') {
          this.clearTaskTimer();
          this.loading.set(false);
          this.message.set(task.error || task.message);
          this.loadHistory();
        } else if (task.status === 'cancelled') {
          this.clearTaskTimer();
          this.loading.set(false);
          this.message.set(task.message || '任务已取消');
          this.loadHistory();
        }
      },
      error: error => this.taskFailed(error),
    });
  }

  private finishTask(task: CrawlTask): void {
    this.clearTaskTimer();
    this.store.setVideos(task.videos);
    this.loading.set(false);
    this.message.set(task.message);
    this.loadHistory();
  }

  private loadSavedKeywords(): void {
    try {
      const raw = localStorage.getItem(this.STORAGE_KEY);
      if (raw) this.keywordInput = raw;
    } catch { this.keywordInput = ''; }
  }

  private saveRecentKeywords(text: string): void {
    localStorage.setItem(this.STORAGE_KEY, text);
  }

  private applyAuthStatus(status: { status: AuthState; message: string }): void {
    this.authState.set(status.status);
    this.authMessage.set(status.message);
  }

  private authFailed(error: unknown): void {
    this.clearAuthTimer();
    this.authState.set('failed');
    this.authMessage.set(this.errorMessage(error));
  }

  private taskFailed(error: unknown): void {
    this.clearTaskTimer();
    this.loading.set(false);
    this.message.set(this.errorMessage(error));
  }

  private errorMessage(error: unknown): string {
    if (error instanceof HttpErrorResponse) return error.error?.detail || '无法连接采集服务，请确认后端已启动';
    return '发生未知错误';
  }

  private clearTimers(): void { this.clearAuthTimer(); this.clearTaskTimer(); }
  private clearAuthTimer(): void { this.authSub?.unsubscribe(); this.authSub = undefined; }
  private clearTaskTimer(): void { this.taskSub?.unsubscribe(); this.taskSub = undefined; }
}
