import { Injectable, computed, signal } from '@angular/core';

export interface VideoItem {
  id: number;
  title: string;
  author: string;
  platform: string;
  keyword: string;
  duration: string;
  views: string;
  selected: boolean;
  palette: string;
  analysis: string;
  tags: string[];
  thumbnailUrl?: string;
  url?: string;
  likes?: number;
  publishedAt?: string;
  mediaUrl?: string;
}

const STORAGE_KEY = 'if_sample_videos';

@Injectable({ providedIn: 'root' })
export class VideoStore {
  readonly videos = signal<VideoItem[]>(this._load());
  readonly analyzed = signal<VideoItem[]>([]);
  readonly selectedCount = computed(() => this.videos().filter(video => video.selected).length);
  readonly selectedVideos = computed(() => this.videos().filter(video => video.selected));

  private _load(): VideoItem[] {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const data: VideoItem[] = JSON.parse(raw);
        return data.map(item => ({ ...item, selected: false }));
      }
    } catch {}
    return [];
  }

  private _save(): void {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(this.videos())); } catch {}
  }

  setVideos(items: Array<{
    id: number; title: string; author: string; platform: string; keyword: string;
    duration?: string; views?: string; thumbnail_url?: string; url?: string; likes?: number; published_at?: string; media_url?: string;
  }>): void {
    this.videos.set(items.map((item, index) => ({
      id: item.id, title: item.title, author: item.author || '平台用户', platform: item.platform,
      keyword: item.keyword, duration: item.duration || '--:--', views: item.views || '--', selected: false,
      palette: `tone-${index % 6}`, thumbnailUrl: item.thumbnail_url, url: item.url,
      likes: item.likes, publishedAt: item.published_at,
      mediaUrl: item.media_url,
      analysis: '该视频尚未进行内容分析。', tags: ['待分析'],
    })));
    this._save();
  }

  clearVideos(): void {
    this.videos.set([]);
    try { localStorage.removeItem(STORAGE_KEY); } catch {}
  }

  toggle(id: number): void { this.videos.update(items => items.map(item => item.id === id ? { ...item, selected: !item.selected } : item)); }
  selectAll(selected: boolean): void { this.videos.update(items => items.map(item => ({ ...item, selected }))); }
  prepareAnalysis(): boolean {
    const selected = this.videos().filter(video => video.selected);
    if (!selected.length) return false;
    this.analyzed.set(selected);
    return true;
  }
}
