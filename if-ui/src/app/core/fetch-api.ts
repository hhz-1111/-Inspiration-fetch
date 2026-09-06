import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

export type PlatformKey = 'xiaohongshu' | 'douyin';
export type AuthState = 'checking' | 'qr_required' | 'waiting_scan' | 'confirming' | 'authenticated' | 'expired' | 'cancelled' | 'failed';

export interface AuthStatus {
  status: AuthState;
  message: string;
  session_id?: string | null;
}

export interface ApiVideo {
  id: number;
  platform: string;
  keyword: string;
  title: string;
  author: string;
  url?: string;
  thumbnail_url?: string;
  duration?: string;
  views?: string;
  likes?: number;
  published_at?: string;
  media_url?: string;
  is_inspiration?: boolean | number;
}

export interface AnalysisItem extends ApiVideo {
  analysis_item_id: number;
  task_id: string;
  added_at: string;
}

export interface InspirationAnalysis extends ApiVideo {
  analysis_id: number;
  analysis_status: 'pending' | 'processing' | 'completed' | 'failed';
  analysis_text?: string;
  analysis_error?: string;
  marked_at: string;
  analysis_updated_at: string;
  transcript?: string;
  video_title?: string;
  copy_structure?: string;
  boost_points?: string;
  model?: string;
}

export interface CrawlTask {
  id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  message: string;
  error?: string;
  videos: ApiVideo[];
}

export interface CrawlHistoryItem {
  id: string;
  platform: string;
  keywords: string[];
  requested_count: number;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  progress: number;
  message: string;
  error?: string;
  created_at: string;
  updated_at: string;
}

export interface PublicConfig {
  crawl_speeds: Array<{ value: string; label: string; delay_seconds: number }>;
}

@Injectable({ providedIn: 'root' })
export class FetchApi {
  private readonly http = inject(HttpClient);
  private readonly baseUrl = '/api';

  publicConfig(): Observable<PublicConfig> {
    return this.http.get<PublicConfig>(`${this.baseUrl}/config/public`);
  }

  authStatus(platform: PlatformKey): Observable<AuthStatus> {
    return this.http.get<AuthStatus>(`${this.baseUrl}/platform-auth/${platform}/status`);
  }
  startAuth(platform: PlatformKey): Observable<AuthStatus> {
    return this.http.post<AuthStatus>(`${this.baseUrl}/platform-auth/${platform}/sessions`, {});
  }
  sessionStatus(sessionId: string): Observable<AuthStatus> {
    return this.http.get<AuthStatus>(`${this.baseUrl}/platform-auth/sessions/${sessionId}/status`);
  }
  qrUrl(sessionId: string, version = 0): string {
    return `${this.baseUrl}/platform-auth/sessions/${sessionId}/qrcode?v=${version}`;
  }
  logout(platform: PlatformKey): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/platform-auth/${platform}`);
  }
  startCrawl(platform: PlatformKey, keywords: string[], count: number, createdTime: string, minLikes: number, speed: string): Observable<{ id: string; status: string }> {
    return this.http.post<{ id: string; status: string }>(`${this.baseUrl}/crawl-tasks`, { platform, keywords, count, created_time: createdTime, min_likes: minLikes, speed });
  }
  crawlStatus(taskId: string): Observable<CrawlTask> {
    return this.http.get<CrawlTask>(`${this.baseUrl}/crawl-tasks/${taskId}`);
  }
  crawlHistory(): Observable<{ tasks: CrawlHistoryItem[] }> {
    return this.http.get<{ tasks: CrawlHistoryItem[] }>(`${this.baseUrl}/crawl-tasks`);
  }
  cancelCrawl(taskId: string): Observable<{ status: string; message: string }> {
    return this.http.post<{ status: string; message: string }>(`${this.baseUrl}/crawl-tasks/${taskId}/cancel`, null);
  }
  enqueueAnalysis(videoIds: number[]): Observable<{ added: number; total: number }> {
    return this.http.post<{ added: number; total: number }>(`${this.baseUrl}/analysis-items`, { video_ids: videoIds });
  }
  analysisItems(): Observable<{ items: AnalysisItem[] }> {
    return this.http.get<{ items: AnalysisItem[] }>(`${this.baseUrl}/analysis-items`);
  }
  parseSharedLink(platform: PlatformKey | 'auto', url: string): Observable<{ item: AnalysisItem }> {
    return this.http.post<{ item: AnalysisItem }>(`${this.baseUrl}/analysis-items/parse-share`, { platform, url });
  }
  uploadLocalVideo(file: File, title = ''): Observable<{ item: AnalysisItem }> {
    return this.http.post<{ item: AnalysisItem }>(`${this.baseUrl}/analysis-items/upload`, file, {
      headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-Upload-Filename': encodeURIComponent(file.name), 'X-Upload-Title': encodeURIComponent(title) },
    });
  }
  discardAnalysisItem(itemId: number): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/analysis-items/${itemId}`);
  }
  deleteVideo(videoId: number): Observable<void> {
    return this.http.delete<void>(`${this.baseUrl}/videos/${videoId}`);
  }
  markInspiration(videoId: number, marked: boolean): Observable<{ marked: boolean }> {
    return this.http.patch<{ marked: boolean }>(`${this.baseUrl}/videos/${videoId}/inspiration`, { marked });
  }
  inspirations(): Observable<{ items: InspirationAnalysis[] }> {
    return this.http.get<{ items: InspirationAnalysis[] }>(`${this.baseUrl}/inspirations`);
  }
  playback(videoId: number): Observable<{ media_url: string }> {
    return this.http.get<{ media_url: string }>(`${this.baseUrl}/videos/${videoId}/playback`);
  }
  getSettings(): Observable<{ mimo_api_key: string; configured: boolean; source: string }> {
    return this.http.get<{ mimo_api_key: string; configured: boolean; source: string }>(`${this.baseUrl}/admin/settings`);
  }
  updateSettings(key: string): Observable<{ mimo_api_key: string; configured: boolean }> {
    return this.http.put<{ mimo_api_key: string; configured: boolean }>(`${this.baseUrl}/admin/settings`, { mimo_api_key: key });
  }
}
