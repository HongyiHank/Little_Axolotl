# sources/youtube.py

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Deque, Tuple
import concurrent.futures

import discord
import yt_dlp as youtube_dl

# 從模組導入設定和錯誤
from utils.config import (
    YDL_OPTIONS, YTDL_TIMEOUT, MAX_PLAYLIST, CACHE_LIMIT,
    CLEANUP_INTERVAL_SECONDS, CACHE_EXPIRE_SECONDS, DEFAULT_VOLUME, VALID_DOMAINS
)
from utils.errors import InvalidURL, MusicError, NetworkError

logger = logging.getLogger('discord.little_axolotl.youtube')

# 獨立的 yt-dlp 提取函數
def run_ytdlp_extraction(url: str, ydl_opts: dict) -> Dict:
    """
    在獨立執行緒中執行 yt-dlp 提取。
    """
    try:
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        logging.error(f"YTDL-Thread-Error for url {url}: {e}", exc_info=True)
        return {'_type': 'error', 'error': str(e)}

# 輔助類別
class DummyAudioSource(discord.AudioSource):
    """空白音頻源類別"""
    def read(self) -> bytes: return b""
    def is_opus(self) -> bool: return False
    def cleanup(self) -> None: pass

# 音源處理
class YTDLSource(discord.PCMVolumeTransformer):
    """處理來自 yt-dlp 的音源，並轉換為 Discord 可播放格式"""
    _cache: Dict[str, Dict] = {}
    _cache_lock: Optional[asyncio.Lock] = None
    _last_cache_cleanup = 0
    __slots__ = ('original', 'data', 'title', 'duration', 'url', 'lazy', 
                 'webpage_url', '_is_prepared', '_prepare_lock')
    
    def __init__(self, source: Optional[discord.AudioSource], *, data: Dict[str, Any],
                 volume: float = DEFAULT_VOLUME, lazy: bool = True, webpage_url: Optional[str] = None) -> None:
        if source is None:
            source = DummyAudioSource()
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', '未知標題')
        self.duration = data.get('duration', 0)
        self.url = data.get('url')
        self.lazy = lazy
        self.webpage_url = webpage_url or data.get('webpage_url')
        self._is_prepared = not lazy
        self._prepare_lock = asyncio.Lock()

    @classmethod
    async def clear_expired_cache(cls) -> None:
        current_time = time.time()
        if (current_time - cls._last_cache_cleanup < CLEANUP_INTERVAL_SECONDS and len(cls._cache) < CACHE_LIMIT):
            return
        if cls._cache_lock is None: cls._cache_lock = asyncio.Lock()
        async with cls._cache_lock:
            initial_size = len(cls._cache)
            expired_keys = [k for k, v in cls._cache.items() if current_time - v['timestamp'] > CACHE_EXPIRE_SECONDS]
            for key in expired_keys: del cls._cache[key]
            if len(cls._cache) > CACHE_LIMIT:
                sorted_items = sorted(cls._cache.items(), key=lambda item: item[1]['timestamp'])
                items_to_remove = len(cls._cache) - CACHE_LIMIT
                for key, _ in sorted_items[:items_to_remove]: del cls._cache[key]
            cls._last_cache_cleanup = current_time
            if (items_removed := initial_size - len(cls._cache)) > 0:
                logger.debug(f"快取清理完成：移除 {items_removed} 個項目")

    @classmethod
    async def from_url(cls, url: str, *, loop: asyncio.AbstractEventLoop, executor: concurrent.futures.Executor, playlist_offset: int = 0) -> List['YTDLSource']:
        is_url = any(url.startswith(domain) for domain in VALID_DOMAINS)
        is_search_query = not url.startswith(('http://', 'https://'))
        if not is_url and not is_search_query:
            raise InvalidURL("❌ 請提供有效的 YouTube 連結或搜尋關鍵字")
        asyncio.create_task(cls.clear_expired_cache())
        cache_key = f"{url}__{playlist_offset}"
        if cls._cache_lock is None: cls._cache_lock = asyncio.Lock()
        async with cls._cache_lock:
            if cache_key in cls._cache:
                cached = cls._cache[cache_key]
                if time.time() - cached['timestamp'] < CACHE_EXPIRE_SECONDS:
                    logger.debug(f"從緩存載入: {url}")
                    return [cls(None, data=s.data.copy(), lazy=s.lazy, webpage_url=s.webpage_url) for s in cached['sources']]

        last_error = None
        for attempt in range(3):
            try:
                ydl_opts = YDL_OPTIONS.copy()
                if playlist_offset:
                    ydl_opts['playliststart'] = playlist_offset + 1
                    ydl_opts['playlistend'] = playlist_offset + MAX_PLAYLIST
                else:
                    ydl_opts['playlistend'] = MAX_PLAYLIST
                data = await asyncio.wait_for(loop.run_in_executor(executor, run_ytdlp_extraction, url, ydl_opts), timeout=YTDL_TIMEOUT)
                if not data or data.get('_type') == 'error':
                    raise InvalidURL(f"無法獲取音樂資訊: {url} ({data.get('error', '未知錯誤')})")
                sources = cls._process_data(data, playlist_offset)
                if sources:
                    async with cls._cache_lock:
                        cls._cache[cache_key] = {'sources': sources, 'timestamp': time.time()}
                return sources
            except asyncio.TimeoutError as e: last_error = e
            except Exception as e: last_error = e
            if attempt < 2: await asyncio.sleep(1.5 * (attempt + 1))
        
        if isinstance(last_error, asyncio.TimeoutError): raise NetworkError(f"無法處理 URL: {url} (網絡連接超時)") from last_error
        raise InvalidURL(f"無法處理 URL: {url}") from last_error

    @classmethod
    def _process_data(cls, data: Dict, playlist_offset: int = 0) -> List['YTDLSource']:
        sources = []
        if 'entries' in data and data['entries']:
            entries = data['entries'][:MAX_PLAYLIST]
            for entry in entries:
                if entry:
                    webpage_url = entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={entry.get('id')}" if entry.get('id') else None)
                    if webpage_url and webpage_url.startswith(('http://', 'https://')):
                        sources.append(cls(None, data=entry, webpage_url=webpage_url))
        elif data.get('_type') != 'playlist':
            sources.append(cls(None, data=data, webpage_url=data.get('webpage_url')))
        return sources

    async def prepare(self, *, loop: asyncio.AbstractEventLoop, executor: concurrent.futures.Executor) -> None:
        if not self.lazy or self._is_prepared: return
        async with self._prepare_lock:
            if self._is_prepared: return
            last_error = None
            for attempt in range(3):
                try:
                    ydl_opts = YDL_OPTIONS.copy()
                    ydl_opts.update({'playlistend': 1})
                    data = await asyncio.wait_for(loop.run_in_executor(executor, run_ytdlp_extraction, self.webpage_url, ydl_opts), timeout=YTDL_TIMEOUT)
                    if not data or data.get('_type') == 'error':
                        raise MusicError(f"無法獲取音軌信息: {self.webpage_url}")
                    self.data.update(data)
                    if data.get('url'):
                        self.url = data['url']
                        self.original = self._create_audio_source(data)
                        self.source = self.original
                        self._is_prepared = True
                        return
                    # 若未獲取到 direct URL，則嘗試完整提取
                    ydl_opts['extract_flat'] = False
                    full_data = await asyncio.wait_for(loop.run_in_executor(executor, run_ytdlp_extraction, self.webpage_url, ydl_opts), timeout=YTDL_TIMEOUT)
                    if not full_data or not full_data.get('url'):
                        raise MusicError(f"無法獲取音頻 URL: {self.webpage_url}")
                    self.url = full_data['url']
                    self.duration = full_data.get('duration', self.duration)
                    self.title = full_data.get('title', self.title)
                    self.original = self._create_audio_source(full_data)
                    self.source = self.original
                    self.data.update(full_data)
                    self._is_prepared = True
                    return
                except asyncio.TimeoutError as e: last_error = e
                except Exception as e: last_error = e
                if attempt < 2: await asyncio.sleep(1.5 * (attempt + 1))
            if isinstance(last_error, asyncio.TimeoutError): raise NetworkError(f"無法加載音軌: {self.title} (連接超時)") from last_error
            raise MusicError(f"無法加載音軌: {self.title}") from last_error
    
    @classmethod
    def _create_audio_source(cls, data: Dict) -> Optional[discord.FFmpegPCMAudio]:
        url = data.get('url')
        if not url: raise InvalidURL("無法獲取音頻 URL")
        return create_ffmpeg_audio(url, start=0.0)

    def cleanup(self) -> None:
        if self.original and hasattr(self.original, 'cleanup'):
            self.original.cleanup()
        super().cleanup()

def create_ffmpeg_audio(url: str, start: float = 0.0) -> discord.FFmpegPCMAudio:
    """建立 FFmpeg 音源實例"""
    before_options = '-thread_queue_size 8192'
    if start > 0:
        before_options += f" -ss {start}"
    before_options += ' -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 5000000'
    processing_options = '-vn -loglevel error'
    try:
        return discord.FFmpegPCMAudio(url, before_options=before_options, options=processing_options)
    except FileNotFoundError:
        raise MusicError("找不到 FFmpeg 執行檔")
    except Exception as e:
        raise MusicError(f"音頻源建立失敗：{e}") from e

def adjust_sources(sources: List[YTDLSource], current_queue_length: int) -> Tuple[List[YTDLSource], bool, int]:
    """裁剪音源列表以符合隊列容量"""
    available_slots = MAX_QUEUE_LENGTH - current_queue_length
    was_truncated = False
    if len(sources) > available_slots:
        sources = sources[:available_slots]
        was_truncated = True
    return sources, was_truncated, available_slots