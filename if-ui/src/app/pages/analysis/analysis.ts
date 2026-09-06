import { Component, computed, HostListener, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { Router } from '@angular/router';
import { AnalysisItem, FetchApi, PlatformKey } from '../../core/fetch-api';

interface KeywordGroup {
  keyword: string;
  items: AnalysisItem[];
}

@Component({ selector: 'app-analysis', imports: [RouterLink, FormsModule], templateUrl: './analysis.html', styleUrl: './analysis.scss' })
export class AnalysisPage {
  private readonly api = inject(FetchApi);
  private readonly router = inject(Router);
  readonly items = signal<AnalysisItem[]>([]);
  readonly loading = signal(true);
  readonly error = signal('');
  readonly openKeywords = signal<Set<string>>(new Set());
  readonly playbackUrl = signal(''); readonly playbackLoading = signal(false); readonly playbackError = signal('');
  readonly updatingIds = signal<Set<number>>(new Set());
  readonly selectedIds = signal<Set<number>>(new Set());
  readonly shareDialogOpen = signal(false);
  readonly shareLoading = signal(false);
  readonly shareError = signal('');
  readonly uploadMode = signal<'link' | 'file'>('link');
  readonly fileDragging = signal(false);          // 正在把文件拖进本页
  readonly parseProgress = signal<{ done: number; total: number } | null>(null); // 多条链接解析进度
  selectedFile: File | null = null;
  uploadTitle = '';
  sharePlatform: PlatformKey | 'auto' = 'auto';
  shareUrl = '';

  readonly groups = computed<KeywordGroup[]>(() => {
    const grouped = new Map<string, AnalysisItem[]>();
    for (const item of this.items()) {
      const entries = grouped.get(item.keyword) ?? [];
      entries.push(item);
      grouped.set(item.keyword, entries);
    }
    return [...grouped.entries()].map(([keyword, items]) => ({ keyword, items }));
  });

  constructor() {
    this.load();
  }

  load(): void {
    this.loading.set(true);
    this.error.set('');
    this.api.analysisItems().subscribe({
      next: response => {
        this.items.set(response.items);
        this.openKeywords.set(new Set(response.items.length ? [response.items[0].keyword] : []));
        this.loading.set(false);
      },
      error: () => {
        this.error.set('无法加载待分析视频，请确认后端服务已启动');
        this.loading.set(false);
      },
    });
  }

  toggleGroup(keyword: string): void {
    this.openKeywords.update(current => {
      const next = new Set(current);
      next.has(keyword) ? next.delete(keyword) : next.add(keyword);
      return next;
    });
  }

  openShareDialog(): void { this.shareError.set(''); this.uploadMode.set('link'); this.selectedFile = null; this.uploadTitle = ''; this.shareDialogOpen.set(true); }
  closeShareDialog(): void { if (!this.shareLoading()) this.shareDialogOpen.set(false); }

  /** 从粘贴文本里提取所有分享链接（去重），支持每行一条或整段文案混贴。 */
  private extractShareUrls(text: string): string[] {
    const urls = (text.match(/https?:\/\/[^\s，。！？、；;"'<>【】（）()]+/g) ?? [])
      .map(u => u.replace(/[），。！？、；;》】"')\]]+$/, '').trim())
      .filter(u => u.length >= 8);
    return [...new Set(urls)];
  }

  private shareErrorText(error: unknown): string {
    return error instanceof HttpErrorResponse ? error.error?.detail || '链接解析失败' : '链接解析失败';
  }

  parseShareLink(): void {
    const urls = this.extractShareUrls(this.shareUrl);
    if (!urls.length) { this.shareError.set('请粘贴抖音/小红书分享链接或完整分享文案'); return; }
    if (urls.length > 20) { this.shareError.set(`一次最多解析 20 条，当前共 ${urls.length} 条，请分批粘贴`); return; }
    this.shareLoading.set(true); this.shareError.set('');
    this.parseProgress.set({ done: 0, total: urls.length });
    const added: AnalysisItem[] = [];
    const failed: Array<{ url: string; reason: string }> = [];
    let index = 0;
    const next = (): void => {
      if (index >= urls.length) {
        this.shareLoading.set(false);
        this.parseProgress.set(null);
        if (added.length) {
          this.items.update(items => [...added, ...items]);
          this.openKeywords.update(current => new Set([...current, ...added.map(item => item.keyword)]));
        }
        if (failed.length) {
          const reasons = failed.slice(0, 3).map(f => `${f.url.slice(0, 60)}：${f.reason}`).join('；');
          this.shareError.set(`成功 ${added.length} 条，失败 ${failed.length} 条。${reasons}${failed.length > 3 ? '…' : ''}`);
          this.shareUrl = failed.map(f => f.url).join('\n');
          return;
        }
        this.shareDialogOpen.set(false);
        this.shareUrl = '';
        return;
      }
      const url = urls[index];
      this.parseProgress.set({ done: index, total: urls.length });
      this.api.parseSharedLink(this.sharePlatform, url).subscribe({
        next: response => {
          added.push(response.item);
          index += 1;
          this.parseProgress.set({ done: index, total: urls.length });
          next();
        },
        error: error => {
          failed.push({ url, reason: this.shareErrorText(error) });
          index += 1;
          this.parseProgress.set({ done: index, total: urls.length });
          next();
        },
      });
    };
    next();
  }
  selectUploadMode(mode: 'link' | 'file'): void { this.uploadMode.set(mode); this.shareError.set(''); }

  /** 手动上传对话框主按钮文案（含多条链接解析进度）。 */
  shareActionLabel(): string {
    if (this.uploadMode() === 'file') return this.shareLoading() ? '上传中…' : '上传视频';
    if (this.shareLoading()) {
      const progress = this.parseProgress();
      if (progress && progress.done > 0) return `解析中 ${progress.done}/${progress.total}…`;
      return '解析中…';
    }
    return '解析并加入队列';
  }
  fileChanged(event: Event): void {
    const file = (event.target as HTMLInputElement).files?.[0] ?? null;
    if (!file) return;
    if (!this.isVideoFile(file)) { this.shareError.set(`“${file.name}”不是支持的视频格式，请选择 MP4 / MOV / WebM / M4V`); return; }
    if (file.size > 500 * 1024 * 1024) { this.shareError.set('视频文件不能超过 500 MB'); return; }
    this.shareError.set('');
    this.selectedFile = file;
  }
  uploadLocalVideo(): void {
    if (!this.selectedFile) { this.shareError.set('请先选择或拖入本地视频文件'); return; }
    this.shareLoading.set(true); this.shareError.set('');
    this.api.uploadLocalVideo(this.selectedFile, this.uploadTitle).subscribe({
      next: response => { this.items.update(items => [response.item, ...items]); this.openKeywords.update(current => new Set([...current, response.item.keyword])); this.shareLoading.set(false); this.shareDialogOpen.set(false); this.selectedFile = null; this.uploadTitle = ''; },
      error: error => { this.shareError.set(error instanceof HttpErrorResponse ? error.error?.detail || '上传失败' : '上传失败'); this.shareLoading.set(false); },
    });
  }

  // ---- 本地视频拖拽上传（整页任意位置松开即上传） ----
  private static readonly VIDEO_EXT_RE = /\.(mp4|mov|webm|m4v)$/i;

  private dragHasFiles(event: DragEvent): boolean {
    return !!event.dataTransfer && Array.from(event.dataTransfer.types).includes('Files');
  }

  private isVideoFile(file: File): boolean {
    return file.type.startsWith('video/') || AnalysisPage.VIDEO_EXT_RE.test(file.name);
  }

  @HostListener('document:dragover', ['$event'])
  onDocDragOver(event: DragEvent): void {
    if (!this.dragHasFiles(event)) return;
    event.preventDefault();
    this.fileDragging.set(true);
  }

  @HostListener('document:dragleave', ['$event'])
  onDocDragLeave(event: DragEvent): void {
    if (!event.relatedTarget) this.fileDragging.set(false);
  }

  @HostListener('document:dragcancel')
  onDocDragCancel(): void {
    this.fileDragging.set(false);
  }

  @HostListener('document:drop', ['$event'])
  onDocDrop(event: DragEvent): void {
    if (!this.dragHasFiles(event)) return;
    event.preventDefault();
    this.fileDragging.set(false);
    if (this.shareLoading()) return;
    const file = Array.from(event.dataTransfer?.files ?? []).find(f => this.isVideoFile(f)) ?? null;
    this.acceptDroppedVideo(file, event.dataTransfer?.files ?? null);
  }

  private acceptDroppedVideo(file: File | null, files: FileList | null): void {
    if (!this.shareDialogOpen()) { this.shareError.set(''); this.shareDialogOpen.set(true); }
    this.uploadMode.set('file');
    if (!file && files && files.length) {
      this.shareError.set(`不支持该文件格式，请拖入 MP4 / MOV / WebM / M4V 视频`);
      return;
    }
    if (!file) { this.shareError.set('未识别到本地视频文件'); return; }
    this.prepareLocalVideo(file);
  }

  private prepareLocalVideo(file: File): void {
    if (this.shareLoading()) return;
    if (!this.isVideoFile(file)) {
      this.shareError.set(`“${file.name}”不是支持的视频格式，请选择 MP4 / MOV / WebM / M4V`);
      return;
    }
    if (file.size > 500 * 1024 * 1024) {
      this.shareError.set('视频文件不能超过 500 MB');
      return;
    }
    this.shareError.set('');
    if (!this.shareDialogOpen()) this.shareDialogOpen.set(true);
    this.uploadMode.set('file');
    this.selectedFile = file;
    this.uploadLocalVideo();
  }

  isOpen(keyword: string): boolean {
    return this.openKeywords().has(keyword);
  }

  discard(event: Event, item: AnalysisItem): void {
    event.stopPropagation();
    if (!window.confirm(`确定永久删除“${item.title}”吗？该操作会同时删除采集记录，且无法恢复。`)) return;
    this.setUpdating(item.id, true);
    this.api.deleteVideo(item.id).subscribe({
      next: () => {
        this.items.update(items => items.filter(current => current.analysis_item_id !== item.analysis_item_id));
        this.setUpdating(item.id, false);
      },
      error: () => { this.error.set('永久删除失败，请稍后重试'); this.setUpdating(item.id, false); },
    });
  }

  markAsInspiration(event: Event, item: AnalysisItem): void {
    event.stopPropagation();
    this.setUpdating(item.id, true);
    this.api.markInspiration(item.id, true).subscribe({
      next: response => {
        if (response.marked) this.items.update(items => items.filter(current => current.analysis_item_id !== item.analysis_item_id));
        this.setUpdating(item.id, false);
        this.router.navigateByUrl('/inspirations');
      },
      error: (err: HttpErrorResponse) => { this.error.set(err.error?.detail || '灵感标记失败，请稍后重试'); this.setUpdating(item.id, false); },
    });
  }

  toggleSelection(event: Event, id: number): void {
    event.stopPropagation();
    this.selectedIds.update(current => { const next = new Set(current); next.has(id) ? next.delete(id) : next.add(id); return next; });
  }
  isSelected(id: number): boolean { return this.selectedIds().has(id); }
  selectGroup(items: AnalysisItem[], selected: boolean): void {
    this.selectedIds.update(current => { const next = new Set(current); for (const item of items) selected ? next.add(item.id) : next.delete(item.id); return next; });
  }
  selectedInGroup(items: AnalysisItem[]): number { return items.filter(item => this.isSelected(item.id)).length; }
  analyzeSelected(): void {
    const selected = this.items().filter(item => this.isSelected(item.id));
    if (!selected.length) return;
    selected.forEach(item => this.setUpdating(item.id, true)); this.error.set('');
    const successful = new Set<number>();
    let index = 0;
    const processNext = () => {
      if (index >= selected.length) {
        this.items.update(current => current.filter(item => !successful.has(item.analysis_item_id)));
        this.selectedIds.update(current => new Set([...current].filter(selectedId => !successful.has(selectedId))));
        this.router.navigateByUrl('/inspirations');
        return;
      }
      const item = selected[index++];
      this.api.markInspiration(item.id, true).subscribe({
        next: () => { successful.add(item.analysis_item_id); this.setUpdating(item.id, false); processNext(); },
        error: (err: HttpErrorResponse) => { this.error.set(err.error?.detail || '部分视频分析失败'); this.setUpdating(item.id, false); processNext(); },
      });
    };
    processNext();
  }

  isUpdating(id: number): boolean { return this.updatingIds().has(id); }

  private setUpdating(id: number, updating: boolean): void {
    this.updatingIds.update(current => {
      const next = new Set(current);
      updating ? next.add(id) : next.delete(id);
      return next;
    });
  }

  formatDate(value: string): string {
    return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value));
  }

  padIndex(value: number): string {
    return String(value).padStart(2, '0');
  }

  play(video: AnalysisItem, event?: Event): void {
    event?.stopPropagation();
    this.playbackError.set(''); this.playbackUrl.set(''); this.playbackLoading.set(true);
    this.api.playback(video.id).subscribe({next:r=>{video.media_url=r.media_url;this.playbackUrl.set(r.media_url);this.playbackLoading.set(false);},error:()=>{this.playbackError.set('无法取得播放地址，平台可能要求重新登录');this.playbackLoading.set(false);}});
  }
  closePlayer(): void { this.playbackUrl.set(''); this.playbackError.set(''); this.playbackLoading.set(false); }
}
