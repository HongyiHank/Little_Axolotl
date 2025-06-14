import asyncio
import datetime
import logging
import os
import re
import time
import weakref
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
import concurrent.futures # 用於並行處理 yt-dlp

import discord
from discord import ui
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp as youtube_dl

# 載入環境變數
load_dotenv()

# 環境變數讀取器
def get_env_value(key: str, default: Any, value_type: type = str) -> Any:
    """讀取環境變數，支援類型轉換和預設值"""
    try:
        value = os.getenv(key, str(default))
        if value_type == bool:
            return value.lower() in ('true', '1', 'yes', 'on', 'enabled')
        elif value_type == list:
            return [item.strip() for item in value.split(',') if item.strip()]
        return value_type(value)
    except (ValueError, TypeError):
        logging.warning(f"環境變數 {key} 值無效: {os.getenv(key)}，使用預設值 {default}")
        return default

# 系統配置
YTDL_TIMEOUT = get_env_value('YTDL_TIMEOUT', 30.0, float)
CACHE_LIMIT = get_env_value('CACHE_LIMIT', 1000, int)
MAX_PLAYLIST = get_env_value('MAX_PLAYLIST', 50, int)
BUTTON_TIMEOUT_SECONDS = get_env_value('BUTTON_TIMEOUT', 180, int)
DEFAULT_VOLUME = get_env_value('DEFAULT_VOLUME', 1.0, float)
MAX_QUEUE_LENGTH = get_env_value('MAX_QUEUE_LENGTH', 100, int)
QUEUE_PAGE_SIZE = get_env_value('QUEUE_PAGE_SIZE', 10, int)
IDLE_TIMEOUT_SECONDS = get_env_value('IDLE_TIMEOUT_SECONDS', 180, int)
CHECK_INTERVAL_SECONDS = get_env_value('CHECK_INTERVAL_SECONDS', 30, int)
CACHE_EXPIRE_SECONDS = get_env_value('CACHE_EXPIRE_SECONDS', 3600, int)
CLEANUP_INTERVAL_SECONDS = get_env_value('CLEANUP_INTERVAL_SECONDS', 300, int)
CASE_INSENSITIVE = get_env_value('CASE_INSENSITIVE', True, bool)
ERROR_RATE_LIMIT = get_env_value('ERROR_RATE_LIMIT', 5, int)
ERROR_COOLDOWN = get_env_value('ERROR_COOLDOWN', 60, int)

# 支援的 YouTube 網域
VALID_DOMAINS = tuple(get_env_value('VALID_DOMAINS', [
    'https://www.youtube.com/',
    'https://youtube.com/',
    'https://youtu.be/',
    'https://music.youtube.com/'
], list))

# yt-dlp 核心配置
YDL_OPTIONS = {
    'format': os.getenv('YTDL_FORMAT', 'bestaudio/best'),
    'nocheckcertificate': get_env_value('YTDL_NO_CHECK_CERTIFICATE', True, bool),
    'ignoreerrors': get_env_value('YTDL_IGNORE_ERRORS', True, bool),
    'quiet': get_env_value('YTDL_QUIET', True, bool),
    'no_warnings': get_env_value('YTDL_NO_WARNINGS', True, bool),
    'default_search': 'ytsearch:',
    'source_address': '0.0.0.0',
    'noplaylist': False,
    'skip_download': True,
    'extract_flat': 'in_playlist',
    'lazy_playlist': True,
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.7103.48 Safari/537.36'
    }
}

# Discord 機器人初始化
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    help_command=None,
    case_insensitive=CASE_INSENSITIVE
)

# 為 yt-dlp 建立執行緒池，避免阻塞主事件循環
# max_workers 可根據伺服器 CPU 邏輯核心數調整，建議 4~8，通常不超過 CPU 邏輯核心數
thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)

logger = logging.getLogger('discord.music_bot')

# 獨立的 yt-dlp 提取函數
def run_ytdlp_extraction(url: str, ydl_opts: dict) -> Dict:
    """
    在獨立執行緒中執行 yt-dlp 提取。
    """
    try:
        # 在子執行緒中建立新的 ydl 實例
        with youtube_dl.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as e:
        # 將異常資訊以可序列化的方式傳回主執行緒
        logging.error(f"YTDL-Thread-Error for url {url}: {e}", exc_info=True)
        return {'_type': 'error', 'error': str(e)}


# 錯誤頻率限制器
class ErrorRateLimiter:
    """限制特定伺服器和錯誤類型的日誌頻率，避免洗版"""
    def __init__(self):
        self.error_counts = {}
        self.suppressed_errors = set()
    
    def should_log_error(self, guild_id: int, error_type: str) -> bool:
        """檢查特定錯誤是否應被記錄"""
        current_time = time.time()
        
        if guild_id not in self.error_counts:
            self.error_counts[guild_id] = {'count': 0, 'last_reset': current_time}
        
        error_data = self.error_counts[guild_id]
        
        if current_time - error_data['last_reset'] > ERROR_COOLDOWN:
            error_data['count'] = 0
            error_data['last_reset'] = current_time
            self.suppressed_errors.discard(f"{guild_id}_{error_type}")
        
        if error_data['count'] >= ERROR_RATE_LIMIT:
            suppression_key = f"{guild_id}_{error_type}"
            if suppression_key not in self.suppressed_errors:
                self.suppressed_errors.add(suppression_key)
                logger.warning(f"伺服器 {guild_id} 錯誤頻率過高，暫時抑制 {error_type} 錯誤日誌")
            return False
        
        error_data['count'] += 1
        return True

error_limiter = ErrorRateLimiter()

def safe_log_error(guild_id: int, error_type: str, message: str, exc_info=None):
    """安全的錯誤日誌記錄"""
    if error_limiter.should_log_error(guild_id, error_type):
        logger.error(f"[Guild {guild_id}] {message}", exc_info=exc_info)

# 自訂例外
class MusicError(Exception):
    """音樂功能相關錯誤的基底類別"""
    pass

class NetworkError(MusicError):
    """網路連線相關錯誤"""
    pass

class InvalidURL(MusicError):
    """無效 URL 或搜尋查詢錯誤"""
    pass

class QueueFull(MusicError):
    """播放隊列已滿錯誤"""
    def __init__(self):
        super().__init__(f"播放隊列已滿（最大容量：{MAX_QUEUE_LENGTH}）")

# 輔助類別
class DummyAudioSource(discord.AudioSource):
    """空白音頻源類別"""
    def read(self) -> bytes:
        return b""
    def is_opus(self) -> bool:
        return False
    def cleanup(self) -> None:
        pass

# 音源處理
class YTDLSource(discord.PCMVolumeTransformer):
    """處理來自 yt-dlp 的音源，並轉換為 Discord 可播放格式"""
    
    _cache: Dict[str, Dict] = {}
    _cache_lock = asyncio.Lock()
    _last_cache_cleanup = 0
    __slots__ = ('original', 'data', 'title', 'duration', 'url', 'lazy', 
                 'webpage_url', '_is_prepared', '_prepare_lock')
    
    def __init__(self, source: Optional[discord.AudioSource], *, data: Dict[str, Any],
                 volume: float = DEFAULT_VOLUME, lazy: bool = True, webpage_url: Optional[str] = None) -> None:
        """初始化音源實例"""
        if source is None:
            source = DummyAudioSource()
            
        super().__init__(source, volume)
        
        self.original = source
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
        """非同步清理過期的快取項目"""
        current_time = time.time()
        
        if (current_time - cls._last_cache_cleanup < CLEANUP_INTERVAL_SECONDS and
            len(cls._cache) < CACHE_LIMIT):
            return
        
        try:
            async with cls._cache_lock:
                initial_size = len(cls._cache)
                
                # 清理過期項目
                expired_keys = [
                    key for key, data in cls._cache.items()
                    if current_time - data['timestamp'] > CACHE_EXPIRE_SECONDS
                ]
                
                for key in expired_keys:
                    del cls._cache[key]
                    
                # 容量控制
                if len(cls._cache) > CACHE_LIMIT:
                    sorted_items = sorted(
                        cls._cache.items(),
                        key=lambda item: item[1]['timestamp']
                    )
                    
                    items_to_remove = len(cls._cache) - CACHE_LIMIT
                    for key, _ in sorted_items[:items_to_remove]:
                        del cls._cache[key]
                        
                cls._last_cache_cleanup = current_time
                final_size = len(cls._cache)
                items_removed = initial_size - final_size
                
                if items_removed > 0:
                    logger.debug(f"快取清理完成：移除 {items_removed} 個項目，當前快取大小：{final_size}/{CACHE_LIMIT}")
                    
        except Exception as e:
            logger.error(f"快取清理過程發生錯誤：{e}")

    @classmethod
    async def from_url(cls, url: str, *, playlist_offset: int = 0) -> List['YTDLSource']:
        """從 URL 或搜尋查詢建立音源列表"""
        is_url = any(url.startswith(domain) for domain in VALID_DOMAINS)
        is_search_query = not url.startswith(('http://', 'https://'))
        
        if not is_url and not is_search_query:
            raise InvalidURL("❌ 請提供有效的 YouTube 連結或搜尋關鍵字")
            
        asyncio.create_task(cls.clear_expired_cache())
        
        cache_key = f"{url}__{playlist_offset}"
        
        try:
            async with cls._cache_lock:
                if cache_key in cls._cache:
                    cached = cls._cache[cache_key]
                    cache_age = time.time() - cached['timestamp']
                    
                    if cache_age < CACHE_EXPIRE_SECONDS:
                        logger.debug(f"從緩存載入: {url} (緩存年齡: {cache_age:.1f}秒)")
                        return [cls(None, data=s.data.copy(), lazy=s.lazy, webpage_url=s.webpage_url) for s in cached['sources']]
        except Exception as e:
            logger.warning(f"檢查緩存時發生錯誤: {e}")

        retries = 2
        last_error = None
        
        for attempt in range(retries + 1):
            try:
                ydl_opts = YDL_OPTIONS.copy()
                
                if playlist_offset:
                    ydl_opts['playliststart'] = playlist_offset + 1
                    ydl_opts['playlistend'] = playlist_offset + MAX_PLAYLIST
                else:
                    ydl_opts['playlistend'] = MAX_PLAYLIST

                # 在執行緒池中執行 yt-dlp
                data = await asyncio.wait_for(
                    bot.loop.run_in_executor(
                        thread_executor,
                        run_ytdlp_extraction,
                        url,
                        ydl_opts
                    ),
                    timeout=YTDL_TIMEOUT
                )
                
                if not data or data.get('_type') == 'error':
                    error_info = data.get('error', '未知錯誤')
                    raise InvalidURL(f"無法獲取音樂資訊: {url} ({error_info})")
                    
                sources = cls._process_data(data, playlist_offset)
                
                if sources:
                    try:
                        async with cls._cache_lock:
                            cls._cache[cache_key] = {
                                'sources': sources,
                                'timestamp': time.time()
                            }
                            logger.debug(f"已緩存 {len(sources)} 個音頻源，索引: {cache_key}")
                    except Exception as e:
                        logger.error(f"將源寫入緩存時發生錯誤: {e}")
                        
                return sources
                
            except asyncio.TimeoutError as e:
                last_error = e
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
            except Exception as e:
                last_error = e
                if attempt < retries:
                    await asyncio.sleep(1 * (attempt + 1))
        
        error_msg = f"無法處理 URL: {url}"
        if isinstance(last_error, asyncio.TimeoutError):
            raise NetworkError(f"{error_msg} (網絡連接超時，請檢查網路並稍後再試)") from last_error
        elif isinstance(last_error, youtube_dl.utils.DownloadError):
            raise InvalidURL(f"{error_msg} (影片可能不存在或訪問受限)") from last_error
        else:
            raise InvalidURL(f"{error_msg} ({type(last_error).__name__}: {str(last_error)})") from last_error

    @classmethod
    def _process_data(cls, data: Dict, playlist_offset: int = 0) -> List['YTDLSource']:
        """處理 yt-dlp 回傳的資料"""
        sources = []
        processed_count = 0
        skipped_count = 0
        
        try:
            if 'entries' in data and data['entries']:
                entries = data['entries']
                original_count = len(entries)
                
                if playlist_offset and len(entries) > MAX_PLAYLIST:
                    entries = entries[playlist_offset:playlist_offset+MAX_PLAYLIST]
                    logger.debug(f"播放清單已被切片: 從 {playlist_offset} 開始，限制為 {MAX_PLAYLIST} 項")
                else:
                    entries = entries[:MAX_PLAYLIST]
                    if original_count > MAX_PLAYLIST:
                        logger.debug(f"播放清單被截斷: {original_count} -> {len(entries)} 項")
                
                for entry in entries:
                    if not entry:
                        skipped_count += 1
                        continue
                    
                    entry_id = entry.get('id', '')
                    webpage_url = entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={entry_id}" if entry_id else None)
                    
                    if not webpage_url or not webpage_url.startswith(('http://', 'https://')):
                        logger.debug(f"跳過無效項目: {entry.get('title', '未知標題')}")
                        skipped_count += 1
                        continue
                    
                    partial_info = {
                        'title': entry.get('title', 'Unknown Track'),
                        'duration': entry.get('duration', 0),
                        'webpage_url': webpage_url
                    }
                    
                    if 'uploader' in entry: partial_info['uploader'] = entry['uploader']
                    if 'thumbnail' in entry: partial_info['thumbnail'] = entry['thumbnail']
                        
                    source = cls(None, data=partial_info, webpage_url=webpage_url)
                    sources.append(source)
                    processed_count += 1
            
            elif data.get('_type') != 'playlist':
                webpage_url = data.get('webpage_url', '')
                source = cls(None, data=data, webpage_url=webpage_url)
                sources.append(source)
                processed_count += 1
                
        except Exception as e:
            logger.error(f"處理數據時發生錯誤: {e}")
            
        if processed_count > 0 or skipped_count > 0:
            logger.debug(f"處理了 {processed_count} 個音頻源，跳過了 {skipped_count} 個無效項目")
            
        return sources

    @classmethod
    def _create_audio_source(cls, data: Dict) -> Optional[discord.FFmpegPCMAudio]:
        """建立 FFmpeg 音源"""
        url = data.get('url')
        if not url:
            raise InvalidURL("無法獲取音頻 URL")
        return create_ffmpeg_audio(url, start=0.0)

    async def prepare(self, *, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """
        準備音源以供播放 (獲取真實串流 URL)。
        """
        if not self.lazy or self._is_prepared:
            return
        async with self._prepare_lock:
            if self._is_prepared:
                return

            retries = 2
            last_error = None
            
            for attempt in range(retries + 1):
                try:
                    ydl_opts = YDL_OPTIONS.copy()
                    ydl_opts.update({'playlistend': 1})
                    
                    # 在執行緒池中執行 yt-dlp
                    partial_data = await asyncio.wait_for(
                        bot.loop.run_in_executor(
                            thread_executor,
                            run_ytdlp_extraction,
                            self.webpage_url,
                            ydl_opts
                        ),
                        timeout=YTDL_TIMEOUT
                    )

                    if not partial_data or partial_data.get('_type') == 'error':
                        error_info = partial_data.get('error', '未知錯誤')
                        raise MusicError(f"無法獲取音軌信息: {self.webpage_url} ({error_info})")
                    
                    self.data.update({
                        'title': partial_data.get('title', self.data.get('title', 'Unknown Title')),
                        'duration': partial_data.get('duration', self.data.get('duration', 0)),
                        'thumbnail': partial_data.get('thumbnail', self.data.get('thumbnail', '')),
                        'uploader': partial_data.get('uploader', self.data.get('uploader', ''))
                    })
                    
                    if partial_data.get('url'):
                        self.url = partial_data['url']
                        new_source = self._create_audio_source(partial_data)
                        if new_source:
                            self.original = new_source
                            self.source = new_source
                        self.data.update(partial_data)
                        self._is_prepared = True
                        return
                    
                    ydl_opts['extract_flat'] = False
                    # 在執行緒池中執行 yt-dlp
                    full_data = await asyncio.wait_for(
                        bot.loop.run_in_executor(
                            thread_executor,
                            run_ytdlp_extraction,
                            self.webpage_url,
                            ydl_opts
                        ),
                        timeout=YTDL_TIMEOUT
                    )

                    if not full_data or not full_data.get('url') or full_data.get('_type') == 'error':
                        error_info = full_data.get('error', '未知錯誤')
                        raise MusicError(f"無法獲取音頻 URL: {self.webpage_url} ({error_info})")

                    self.url = full_data['url']
                    self.duration = full_data.get('duration', self.duration)
                    self.title = full_data.get('title', self.title)
                    
                    new_source = self._create_audio_source(full_data)
                    if new_source:
                        self.original = new_source
                        self.source = new_source
                        
                    self.data.update(full_data)
                    self._is_prepared = True
                    return
                    
                except asyncio.TimeoutError as e:
                    last_error = e
                except Exception as e:
                    last_error = e
                    
                if attempt < retries:
                    await asyncio.sleep(1.5 * (attempt + 1))
            
            error_msg = f"無法加載音軌: {self.title}"
            if isinstance(last_error, asyncio.TimeoutError):
                raise NetworkError(f"{error_msg} (連接超時)") from last_error
            raise MusicError(error_msg) from last_error

    def cleanup(self) -> None:
        """清理資源"""
        if self.original and hasattr(self.original, 'cleanup'):
            self.original.cleanup()
        super().cleanup()


# 播放器狀態管理
class GuildPlayer:
    """管理單一伺服器的播放器狀態"""
    
    __slots__ = (
        'queue', 'volume', 'loop', 'loop_queue', 'now_playing',
        'text_channel', '_voice_client', 'start_time', 'media_offset'
    )
    
    def __init__(self) -> None:
        """初始化播放器狀態"""
        self.queue: Deque[YTDLSource] = deque(maxlen=MAX_QUEUE_LENGTH)
        self.volume: float = DEFAULT_VOLUME
        self.loop: bool = False
        self.loop_queue: bool = False
        self.now_playing: Optional[YTDLSource] = None
        self.start_time: Optional[float] = None
        self.media_offset: float = 0.0
        self.text_channel: Optional[discord.TextChannel] = None
        self._voice_client: Optional[discord.VoiceClient] = None

    @property
    def voice_client(self) -> discord.VoiceClient:
        """安全的語音客戶端存取器"""
        if not self._voice_client or not self._voice_client.is_connected():
            raise MusicError("機器人未連接至語音頻道")
        return self._voice_client

    @voice_client.setter
    def voice_client(self, client: discord.VoiceClient) -> None:
        """設定語音客戶端"""
        self._voice_client = client

    def cleanup(self) -> None:
        """清理播放器狀態"""
        self.queue.clear()
        self.now_playing = None
        self.start_time = None
        self.media_offset = 0.0
        
        if self._voice_client:
            try:
                self._voice_client.stop()
            except Exception:
                pass
            self._voice_client = None

class MusicController:
    """管理所有伺服器播放器的全域控制器"""
    
    def __init__(self) -> None:
        """初始化控制器"""
        self.players: Dict[int, GuildPlayer] = {}

    def get_player(self, guild_id: int) -> GuildPlayer:
        """取得或建立指定伺服器的播放器"""
        return self.players.setdefault(guild_id, GuildPlayer())

music = MusicController()

# 歌曲預加載
async def _preload_track(track: YTDLSource, guild_id: int):
    """在背景預加載音軌。預加載失敗是可接受的，播放時會再次嘗試。"""
    try:
        await track.prepare()
        logger.info(f"✓ [預加載成功] Guild {guild_id}: {track.title}")
    except Exception as e:
        safe_log_error(guild_id, "preload_error", f"預加載失敗: {track.title} - {e}")

def schedule_preload(player: GuildPlayer):
    """為隊列中的下一首歌曲安排預加載任務。"""
    if player.queue and player._voice_client and player._voice_client.is_connected():
        next_track = player.queue[0]
        if next_track.lazy and not next_track._is_prepared:
            guild_id = player._voice_client.guild.id
            logger.debug(f"正在為 Guild {guild_id} 調度預加載: {next_track.title}")
            asyncio.create_task(_preload_track(next_track, guild_id))

# 指令裝飾器與輔助函數
def in_voice_channel() -> Callable:
    """檢查指令使用者是否在語音頻道中"""
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.author.voice:
            await ctx.send("❌ 請先加入語音頻道後再使用此指令！")
            raise commands.CheckFailure("用戶未在語音頻道")
        return True
    return commands.check(predicate)

def ensure_player() -> Callable:
    """確保伺服器播放器實例存在"""
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild:
            await ctx.send("❌ 此指令僅能在伺服器中使用")
            return False
        music.get_player(ctx.guild.id)
        return True
    return commands.check(predicate)

async def safe_send(ctx: commands.Context, content: str = None, **kwargs) -> Optional[discord.Message]:
    """安全地發送訊息，捕捉並記錄 HTTP 錯誤"""
    try:
        return await ctx.send(content, **kwargs)
    except discord.HTTPException as e:
        logger.error(f"訊息發送失敗：{e}")
        return None

def parse_playlist_offset(query: str) -> Tuple[str, int]:
    """從查詢字串中解析播放清單偏移量 (例如 '... -5')"""
    playlist_offset = 0
    
    offset_pattern = re.search(r'\s+\-(\d+)$', query)
    
    if offset_pattern:
        page_number = int(offset_pattern.group(1))
        if page_number >= 1:
            playlist_offset = (page_number - 1) * MAX_PLAYLIST
            query = query[:offset_pattern.start()].strip()
            
    return query, playlist_offset

def adjust_sources(sources: List[YTDLSource], current_queue_length: int) -> Tuple[List[YTDLSource], bool, int]:
    """裁剪音源列表以符合隊列容量"""
    available_slots = MAX_QUEUE_LENGTH - current_queue_length
    was_truncated = False
    
    if len(sources) > available_slots:
        sources = sources[:available_slots]
        was_truncated = True
        
    return sources, was_truncated, available_slots

def create_ffmpeg_audio(url: str, start: float = 0.0) -> discord.FFmpegPCMAudio:
    """建立 FFmpeg 音源實例"""
    
    before_options = '-thread_queue_size 8192'
    
    
    if start > 0:
        before_options += f" -ss {start}"
        
    before_options += ' -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -rw_timeout 5000000'
    
    processing_options = '-vn -loglevel error'
    try:
        return discord.FFmpegPCMAudio(
            url,
            before_options=before_options,
            options=processing_options
        )
    except FileNotFoundError:
        error_msg = "找不到 FFmpeg 執行檔"
        logger.error(error_msg)
        raise MusicError(error_msg)
    except Exception as e:
        error_msg = f"音頻源建立失敗：{e}"
        logger.error(error_msg)
        raise MusicError(error_msg) from e

async def restart_audio(player: 'GuildPlayer', *, current_media_time: float) -> None:
    """從指定時間點重新開始播放"""
    if not player.now_playing or not player.now_playing.url:
        logger.error("重啟播放失敗：無有效的播放音軌或 URL")
        return
        
    try:
        max_duration = player.now_playing.duration or float('inf')
        safe_media_time = max(0, min(current_media_time, max_duration))
        
        new_audio_source = create_ffmpeg_audio(
            player.now_playing.url,
            start=safe_media_time
        )
        
        if player.voice_client.is_playing():
            player.voice_client.stop()
            await asyncio.sleep(0.1)
        
        guild_id = player.voice_client.guild.id
        player.voice_client.play(
            new_audio_source,
            after=setup_after(guild_id)
        )
        
        player.media_offset = safe_media_time
        player.start_time = time.time()
        
        logger.debug(f"音頻重啟成功：從 {safe_media_time:.1f} 秒開始播放")
        
    except Exception as e:
        logger.error(f"音頻重啟失敗：{e}")
        
        if player.text_channel:
            await safe_send(player.text_channel, content=f"⚠️ 播放重啟失敗：{e}")
            
        try:
            if not player.voice_client.is_playing() and player.now_playing:
                fallback_source = create_ffmpeg_audio(player.now_playing.url, start=0.0)
                guild_id = player.voice_client.guild.id
                player.voice_client.play(
                    fallback_source,
                    after=setup_after(guild_id)
                )
                
                player.media_offset = 0.0
                player.start_time = time.time()
                
                logger.info("已使用緊急恢復模式從頭開始播放")
                
        except Exception as recovery_error:
            logger.error(f"緊急恢復也失敗：{recovery_error}")

async def ensure_voice_connection(ctx: commands.Context, player: GuildPlayer) -> GuildPlayer:
    """確保機器人已連接至語音頻道"""
    if not player._voice_client or not player._voice_client.is_connected():
        player.cleanup()
        
        user_channel = ctx.author.voice.channel
        player.voice_client = await user_channel.connect()
        player.text_channel = ctx.channel
        
        idle_checker.reset_timer(ctx.guild.id)
        
        await safe_send(ctx, content=f"✅ 已加入語音頻道 **{user_channel}**")
    
    return player

# 播放邏輯
def setup_after(guild_id: int) -> Callable:
    """建立一個執行緒安全的 'after' 回調函數"""
    def after_callback(error: Optional[Exception]) -> None:
        try:
            bot.loop.call_soon_threadsafe(
                asyncio.create_task,
                after_playing_callback(guild_id, error)
            )
        except Exception as e:
            logger.error(f"播放回調設定失敗：{e}")
    
    return after_callback

async def after_playing_callback(guild_id: int, error: Optional[Exception]) -> None:
    """處理音軌播放完畢或出錯後的邏輯"""
    player = music.get_player(guild_id)
    
    if not player._voice_client or not player._voice_client.is_connected():
        return
        
    try:
        if error:
            error_message = str(error)
            
            non_critical_errors = [
                "Pipe has been ended",
                "Process has been terminated",
                "Connection reset by peer"
            ]
            
            if any(err in error_message for err in non_critical_errors):
                logger.warning(f"播放管道關閉：{error_message}")
            else:
                logger.error(f"播放錯誤：{error_message}")
                if player.text_channel:
                    truncated_error = error_message[:200] + "..." if len(error_message) > 200 else error_message
                    await safe_send(player.text_channel, content=f"⚠️ 播放錯誤：{truncated_error}")
        
        if player.voice_client.is_playing():
            return
            
        if player.loop and player.now_playing:
            return await _replay_current(player)
            
        if player.loop_queue and player.now_playing:
            player.queue.append(player.now_playing)
            
        await _play_next(guild_id, player)
        
    except asyncio.CancelledError:
        pass
    except Exception as e:
        safe_log_error(guild_id, "callback_error", f"播放回調處理異常：{e}")
        
        if player.text_channel:
            await safe_send(player.text_channel, content=f"⚠️ 系統錯誤：{e}")
        
        try:
            if player.queue and not player.voice_client.is_playing():
                await _play_next(guild_id, player)
        except Exception:
            pass

async def _replay_current(player: GuildPlayer) -> None:
    """重播當前歌曲"""
    try:
        await player.now_playing.prepare()
        new_audio_source = create_ffmpeg_audio(player.now_playing.url, start=0.0)
        replayed_source = YTDLSource(new_audio_source, data=player.now_playing.data, volume=player.volume, lazy=False)
        player.now_playing = replayed_source
        guild_id = player.voice_client.guild.id
        player.voice_client.play(
            player.now_playing,
            after=setup_after(guild_id)
        )
        
        player.start_time = time.time()
        player.media_offset = 0.0
        
        logger.debug(f"單曲循環：重播 {player.now_playing.title}")
        
    except Exception as e:
        safe_log_error(player.voice_client.guild.id, "replay_error", f"重播失敗：{e}")
        if player.text_channel:
            await safe_send(player.text_channel, content=f"⚠️ 重播失敗：{e}")
            await _play_next(player.voice_client.guild.id, player)

async def _play_next(guild_id: int, player: GuildPlayer) -> None:
    """播放隊列中的下一首歌曲"""
    if not player._voice_client or not player._voice_client.is_connected():
        return
        
    try:
        if player.voice_client.is_playing():
            player.voice_client.stop()
            await asyncio.sleep(0.05)
            
        while player.queue:
            next_track = player.queue.popleft()
            
            try:
                # 如果預加載失敗或尚未運行，這裡會同步等待加載
                await next_track.prepare()
                    
                if not next_track.url:
                    raise InvalidURL("音頻源 URL 無效或已過期")
                    
                new_audio_source = create_ffmpeg_audio(next_track.url, start=0.0)
                
                next_track.original = new_audio_source
                next_track.source = new_audio_source
                
                player.now_playing = next_track
                player.media_offset = 0.0
                player.start_time = time.time()
                next_track.volume = player.volume
                
                player.voice_client.play(
                    next_track,
                    after=setup_after(guild_id)
                )
                
                if player.text_channel:
                    await display_now_playing_info(player.text_channel, player)
                
                idle_checker.reset_timer(guild_id)
                
                logger.debug(f"開始播放：{next_track.title}")

                # 當前歌曲已開始播放，立即為下一首安排預加載
                schedule_preload(player)
                
                return
                
            except Exception as track_error:
                safe_log_error(guild_id, "track_error", f"音軌準備失敗：{track_error}")
                
                if player.text_channel:
                    error_msg = f"⚠️ 跳過錯誤歌曲：{next_track.title}"
                    if len(str(track_error)) < 100:
                        error_msg += f" ({track_error})"
                    await safe_send(player.text_channel, content=error_msg)
                
                continue
                
        player.now_playing = None
        player.start_time = None
        player.media_offset = 0.0
        
        if player.text_channel:
            await safe_send(player.text_channel, content="✅ 播放完畢")
            
        logger.info(f"伺服器 {guild_id} 播放隊列已完成")
        
    except Exception as e:
        safe_log_error(guild_id, "play_next_error", f"播放下一首歌曲時發生異常：{e}")
        
        if player.text_channel:
            await safe_send(player.text_channel, content=f"⚠️ 播放系統異常：{e}")

# Discord 指令
async def add_song_to_queue(ctx: commands.Context, player: GuildPlayer, sources: List[YTDLSource]) -> None:
    """將歌曲加入播放隊列"""
    try:
        for source in sources:
            player.queue.append(source)
            
        if not player.voice_client.is_playing() and not player.voice_client.is_paused():
            await _play_next(ctx.guild.id, player)
            
    except Exception as e:
        logger.error(f"加入隊列時發生錯誤：{e}")
        await safe_send(ctx, content=f"❌ 加入隊列失敗：{e}")

@bot.command(name='join')
@in_voice_channel()
async def join(ctx: commands.Context) -> None:
    """加入使用者所在的語音頻道"""
    try:
        target_channel = ctx.author.voice.channel
        player = music.get_player(ctx.guild.id)
        
        if ctx.voice_client:
            if ctx.voice_client.channel == target_channel:
                return await safe_send(ctx, content="✅ 機器人已在當前語音頻道")
            await ctx.voice_client.move_to(target_channel)
        else:
            player.voice_client = await target_channel.connect()
            
        player.text_channel = ctx.channel
        idle_checker.reset_timer(ctx.guild.id)
        
        await safe_send(ctx, content=f"✅ 已加入語音頻道 **{target_channel}**")
        
    except Exception as e:
        safe_log_error(ctx.guild.id, "join_error", f"加入語音頻道失敗：{e}")
        await safe_send(ctx, content=f"❌ 加入失敗：{e}")

async def _handle_playlist_addition(ctx: commands.Context, sources: List[YTDLSource], player: GuildPlayer) -> str:
    """處理播放清單加入的通用邏輯"""
    adjusted_sources, was_truncated, available_slots = adjust_sources(sources, len(player.queue))
    
    player.queue.extend(adjusted_sources)
    
    response = _build_play_response(adjusted_sources, player)
    
    if was_truncated:
        response += f"\n⚠️ 播放清單超過容量限制，僅加入前 {available_slots} 首歌曲"
    
    if not player.voice_client.is_playing() and not player.voice_client.is_paused():
        await _play_next(ctx.guild.id, player)
    else:
        # 如果正在播放，且隊列剛從空變成有，則預加載第一首
        if len(player.queue) == len(adjusted_sources):
            schedule_preload(player)
    
    return response

@bot.command(name='play')
@in_voice_channel()
@ensure_player()
async def play(ctx: commands.Context, *, query: str) -> None:
    """播放音樂或加入播放隊列"""
    try:
        player = music.get_player(ctx.guild.id)
        await ensure_voice_connection(ctx, player)

        if len(player.queue) >= MAX_QUEUE_LENGTH:
            raise QueueFull()

        clean_query, playlist_offset = parse_playlist_offset(query)
        
        async with ctx.typing():
            sources = await YTDLSource.from_url(clean_query, playlist_offset=playlist_offset)
            
            if not sources:
                return await safe_send(ctx, content="❌ 未找到任何音樂內容")

            response_message = await _handle_playlist_addition(ctx, sources, player)
            await safe_send(ctx, content=response_message)
        
        idle_checker.reset_timer(ctx.guild.id)
        
    except InvalidURL as e:
        await safe_send(ctx, content=f"❌ 搜尋或 URL 錯誤：{e}")
    except QueueFull as e:
        await safe_send(ctx, content=f"❌ {e}")
    except Exception as e:
        safe_log_error(ctx.guild.id, "play_error", f"播放指令執行錯誤：{e}")
        await safe_send(ctx, content=f"❌ 播放失敗：{e}")

def _build_play_response(sources: List[YTDLSource], player: GuildPlayer) -> str:
    """建立播放回應訊息"""
    if len(sources) == 1:
        return f"✅ 已加入歌曲：**{sources[0].title}**（隊列位置：{len(player.queue)}）"
    else:
        return f"✅ 已加入 {len(sources)} 首歌曲到播放隊列（當前隊列長度：{len(player.queue)}）"

@bot.command(name='leave')
async def leave(ctx: commands.Context) -> None:
    """離開語音頻道並清空播放隊列"""
    try:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
            
        player = music.get_player(ctx.guild.id)
        player.cleanup()
        
        idle_checker.cancel_timer(ctx.guild.id)
        
        await safe_send(ctx, content="👋 已離開語音頻道並清空播放隊列")
        
    except Exception as e:
        safe_log_error(ctx.guild.id, "leave_error", f"離開語音頻道失敗：{e}")
        await safe_send(ctx, content=f"❌ 離開失敗：{e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """處理語音狀態更新事件"""
    # 處理機器人狀態更新
    if member.id == bot.user.id:
        if before.channel is not None and after.channel is None:
            guild_id = member.guild.id
            player = music.players.get(guild_id)
            if player:
                player.cleanup()
                idle_checker.cancel_timer(guild_id)
                logger.info(f"已清空 {member.guild.name} 的播放隊列，因為機器人已退出語音頻道")
        return
    
    # 處理用戶離開語音頻道的情況
    if before.channel is not None and (after.channel is None or after.channel.id != before.channel.id):
        bot_user = member.guild.me
        
        if (bot_user.voice and bot_user.voice.channel and 
            bot_user.voice.channel.id == before.channel.id):
            
            human_count = sum(1 for m in before.channel.members if not m.bot)
            
            if human_count == 0:
                guild_id = member.guild.id
                player = music.players.get(guild_id)
                
                if player and player._voice_client:
                    logger.info(f"所有用戶已離開語音頻道 {before.channel.name}，機器人自動退出")
                    
                    text_channel = player.text_channel
                    
                    try:
                        await player._voice_client.disconnect()
                        player.cleanup()
                        idle_checker.cancel_timer(guild_id)
                        
                        if text_channel:
                            await safe_send(text_channel, content="👋 所有用戶已離開語音頻道，機器人自動退出")
                    except Exception as e:
                        logger.error(f"機器人自動退出時發生錯誤: {e}")

# 播放隊列 Embed 輔助函數
def _create_queue_embed(player: GuildPlayer, page: int) -> Tuple[Optional[discord.Embed], int]:
    """建立播放隊列頁面的 Embed"""
    if not player.queue:
        embed = discord.Embed(
            description="📪 播放隊列是空的",
            color=0xFFC7EA
        )
        return embed, 1

    total_pages = (len(player.queue) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE
    page = max(1, min(page, total_pages))
    start = (page - 1) * QUEUE_PAGE_SIZE
    end = start + QUEUE_PAGE_SIZE
    queue_slice = list(player.queue)[start:end]

    queue_list = []
    for idx, item in enumerate(queue_slice, start=start + 1):
        duration = f"{int(item.duration // 60)}:{int(item.duration % 60):02d}" if item.duration else "未知"
        # 使用 Markdown 建立超連結
        queue_list.append(f"`{idx}.` [**{item.title}**]({item.webpage_url}) `({duration})`")

    embed = discord.Embed(
        title=f"🎵 播放隊列 (第 {page}/{total_pages} 頁)",
        description="\n".join(queue_list) or "沒有內容",
        color=0xFFC7EA
    )
    embed.set_footer(text=f"共 {len(player.queue)} 首歌曲")
    return embed, total_pages


# 播放隊列互動視圖
class QueueView(ui.View):
    """播放隊列的互動按鈕"""
    def __init__(self, guild_id: int, current_page: int, total_pages: int):
        super().__init__(timeout=BUTTON_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        self.current_page = current_page
        self.total_pages = total_pages
        self.update_buttons()

    def update_buttons(self):
        """根據當前頁碼更新按鈕狀態"""
        self.children[0].disabled = self.current_page <= 1
        self.children[1].disabled = self.total_pages <= 1 or self.current_page >= self.total_pages

    async def _update_message(self, interaction: discord.Interaction, new_page: int):
        """處理分頁的訊息更新"""
        player = music.get_player(self.guild_id)
        if not player:
            return await interaction.response.edit_message(content="播放器已失效。", embed=None, view=None)
        
        embed, new_total_pages = _create_queue_embed(player, new_page)
        
        self.current_page = max(1, min(new_page, new_total_pages))
        self.total_pages = new_total_pages
        
        new_view = QueueView(self.guild_id, self.current_page, self.total_pages)
        
        await interaction.response.edit_message(embed=embed, view=new_view)

    @ui.button(label="⬅️ 上一頁", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page - 1)

    @ui.button(label="➡️ 下一頁", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page + 1)
        
    @ui.button(label="🔄 刷新", style=discord.ButtonStyle.primary)
    async def refresh_queue(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page)


@bot.command(name='list')
@ensure_player()
async def list_queue(ctx: commands.Context, page: int = 1):
    """顯示帶有互動按鈕的播放隊列"""
    player = music.get_player(ctx.guild.id)
    
    embed, total_pages = _create_queue_embed(player, page)

    if not player.queue:
        return await safe_send(ctx, embed=embed)

    view = QueueView(ctx.guild.id, page, total_pages)
    await safe_send(ctx, embed=embed, view=view)


@bot.command(name='now')
@ensure_player()
async def now_playing(ctx: commands.Context):
    """顯示當前歌曲資訊"""
    player = music.get_player(ctx.guild.id)
    await display_now_playing_info(ctx, player)

async def display_now_playing_info(ctx_or_channel, player, embed_only=False):
    """顯示當前歌曲資訊"""
    if not player.now_playing:
        if not embed_only:
            await safe_send(ctx_or_channel, content="❌ 目前沒有正在播放的歌曲")
        return None
    
    if player.voice_client.is_paused():
        current_media_time = player.media_offset
    elif player.start_time:
        elapsed = time.time() - player.start_time
        current_media_time = player.media_offset + elapsed
    else:
        current_media_time = player.media_offset

    duration = player.now_playing.duration or 0
    if duration > 0:
        current_media_time = min(current_media_time, duration)
    
    progress_bar = _build_progress_bar(int(current_media_time), int(duration))
    
    song_data = player.now_playing.data or {}
    uploader = song_data.get('uploader', '未知上傳者')
    upload_date = song_data.get('upload_date', '未知')
    
    if upload_date and upload_date != '未知' and len(upload_date) == 8:
        try:
            year, month, day = upload_date[:4], upload_date[4:6], upload_date[6:8]
            upload_date = f"{year}-{month}-{day}"
        except:
            pass
            
    status_text = "暫停中" if player.voice_client.is_paused() else "正在播放"
    embed_color = 0xAAAAAA if player.voice_client.is_paused() else 0xFFC7EA
            
    embed = discord.Embed(
        title=f"🎵 {status_text}",
        description=f"### [{player.now_playing.title}]({player.now_playing.webpage_url})",
        color=embed_color
    )
        
    if player.now_playing.data.get('thumbnail', ''):
        embed.set_thumbnail(url=player.now_playing.data.get('thumbnail', ''))
        
    embed.add_field(name="👤 上傳者", value=f"```{uploader}```", inline=True)
    if upload_date != '未知':
        embed.add_field(name="📅 上傳日期", value=f"```{upload_date}```", inline=True)
    embed.add_field(name="🔊 音量", value=f"```{int(player.volume * 100)}%```", inline=True)
        
    embed.add_field(
        name=f"⏱️ 播放進度",
        value=f"`{_format_time(current_media_time)} / {_format_time(duration)}`\n{progress_bar}",
        inline=False
    )
        
    if player.queue:
        next_song = player.queue[0]
        embed.add_field(
            name="🎵 下一首",
            value=f"```{next_song.title}```\n隊列中還有 {len(player.queue)} 首歌曲",
            inline=False
        )
    else:
        embed.add_field(name="📋 隊列信息", value="隊列中還有 0 首歌曲", inline=False)
        
    if embed_only:
        return embed
        
    controls = PlayerControlsView(player.voice_client.guild.id)
        
    try:
        if isinstance(ctx_or_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread, commands.Context)):
            await ctx_or_channel.send(content=None, embed=embed, view=controls)
        else:
            logger.warning("display_now_playing_info 收到無效的 ctx_or_channel 類型")
    except Exception as e:
        safe_log_error(player.voice_client.guild.id, "now_playing_send", f"發送 'Now Playing' 訊息失敗: {e}")

class PlayerControlsView(ui.View):
    """播放控制按鈕"""
    def __init__(self, guild_id):
        super().__init__(timeout=BUTTON_TIMEOUT_SECONDS)
        self.guild_id = guild_id
        
    @ui.button(label="⏯️ 暫停/播放", style=discord.ButtonStyle.primary)
    async def toggle_playback(self, interaction: discord.Interaction, button: ui.Button):
        player = music.get_player(self.guild_id)
        if not player._voice_client or not player._voice_client.is_connected() or not player.now_playing:
            return await interaction.response.send_message("❌ 機器人未連接語音頻道或沒有歌曲播放", ephemeral=True)
        
        if player.voice_client.is_paused():
            player.voice_client.resume()
            # 恢復計時
            if player.start_time is None:
                player.start_time = time.time()
            idle_checker.reset_timer(self.guild_id)
            await interaction.response.send_message("▶ 已恢復播放", ephemeral=True)
        else:
            player.voice_client.pause()
            # 暫停計時並更新 offset
            if player.start_time is not None:
                elapsed_since_start = time.time() - player.start_time
                player.media_offset += elapsed_since_start
                player.start_time = None # 標記為暫停
            await interaction.response.send_message("⏸ 已暫停播放", ephemeral=True)
            
    @ui.button(label="⏭️ 跳過", style=discord.ButtonStyle.primary)
    async def skip_track(self, interaction: discord.Interaction, button: ui.Button):
        player = music.get_player(self.guild_id)
        if not player._voice_client or not player._voice_client.is_connected():
            return await interaction.response.send_message("❌ 機器人未連接語音頻道", ephemeral=True)
            
        if not player.now_playing and not player.queue:
            return await interaction.response.send_message("❌ 目前沒有正在播放的歌曲或隊列", ephemeral=True)
            
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        else: # 如果因為某種原因停止了但隊列仍在
            await _play_next(self.guild_id, player)
            
        idle_checker.reset_timer(self.guild_id)
        await interaction.response.send_message("⏭️ 已跳過當前歌曲", ephemeral=True)
    
    @ui.button(label="🔁 單曲循環", style=discord.ButtonStyle.primary)
    async def toggle_loop(self, interaction: discord.Interaction, button: ui.Button):
        player = music.get_player(self.guild_id)
        player.loop = not player.loop
        
        if player.loop:
            await interaction.response.send_message("🔁 單曲循環已啟用", ephemeral=True)
        else:
            await interaction.response.send_message("⏹ 單曲循環已停用", ephemeral=True)

    @ui.button(label="🚪 離開", style=discord.ButtonStyle.danger)
    async def leave_channel(self, interaction: discord.Interaction, button: ui.Button):
        try:
            player = music.get_player(self.guild_id)
            
            if not player.voice_client or not player.voice_client.is_connected():
                return await interaction.response.send_message("❌ 機器人未連接語音頻道", ephemeral=True)
                
            await player.voice_client.disconnect()
            player.cleanup()
            idle_checker.cancel_timer(self.guild_id)
            
            await interaction.response.send_message("👋 已離開語音頻道", ephemeral=True)
        except Exception as e:
            safe_log_error(self.guild_id, "leave_button", f"離開頻道按鈕失敗: {e}")
            await interaction.response.send_message(f"❌ 離開頻道失敗: {e}", ephemeral=True)

def _format_time(seconds: float) -> str:
    """格式化時間顯示"""
    seconds = int(seconds)
    if seconds < 0:
        return "未知"
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"

def _build_progress_bar(elapsed: int, total: int) -> str:
    """建立進度條"""
    if total <= 0:
        return "▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰"
    progress = min(elapsed / total, 1.0)
    bar_length = 20
    position = max(int(bar_length * progress), 0)
    
    filled = "▰" * position
    empty = "▱" * (bar_length - position)
    progress_bar = filled + empty
    percentage = f" {int(progress * 100)}%"
    return progress_bar + percentage

@bot.command(name='volume')
@ensure_player()
async def set_volume(ctx: commands.Context, volume: Optional[str] = None):
    """調整音量 (0 - 200) 或顯示當前音量"""
    player = music.get_player(ctx.guild.id)
    if volume is None:
        return await safe_send(ctx, content=f"🔊 當前音量為 {int(player.volume * 100)}")
    try:
        vol_value = float(volume)
    except ValueError:
        return await safe_send(ctx, content=f"🔊 當前音量為 {int(player.volume * 100)}")

    if not 0 <= vol_value <= 200:
        return await safe_send(ctx, content="❌ 音量必須在 0 到 200 之間")

    player.volume = vol_value / 100.0
    if player.now_playing:
        player.now_playing.volume = player.volume

    idle_checker.reset_timer(ctx.guild.id)
    await safe_send(ctx, content=f"🔊 音量已設定為 {int(vol_value)}")

@bot.command(name='pause')
@ensure_player()
async def toggle_pause(ctx: commands.Context):
    """暫停/恢復播放"""
    player = music.get_player(ctx.guild.id)
    if not player._voice_client or not player.now_playing:
        return await safe_send(ctx, content="❌ 目前沒有正在播放的歌曲")

    if player.voice_client.is_paused():
        player.voice_client.resume()
        # 恢復計時
        if player.start_time is None:
            player.start_time = time.time()
        idle_checker.reset_timer(ctx.guild.id)
        await safe_send(ctx, content="▶ 已恢復播放")
    else:
        player.voice_client.pause()
        # 暫停計時並更新 offset
        if player.start_time is not None:
            elapsed_since_start = time.time() - player.start_time
            player.media_offset += elapsed_since_start
            player.start_time = None  # 標記為暫停
        await safe_send(ctx, content="⏸ 已暫停播放")

@bot.command(name='skip')
@ensure_player()
async def skip(ctx: commands.Context, skip_count: str = "1"):
    """跳過歌曲"""
    try:
        count = int(skip_count)
        if count < 1:
            raise ValueError
    except ValueError:
        return await safe_send(ctx, content="❌ 請輸入正確的數字")

    player = music.get_player(ctx.guild.id)
    if not player._voice_client or not player._voice_client.is_connected():
        return await safe_send(ctx, content="❌ 機器人目前未連接語音頻道")
    if not player.now_playing and not player.queue:
        return await safe_send(ctx, content="❌ 目前沒有正在播放的歌曲或隊列")

    for _ in range(count - 1):
        if player.queue:
            player.queue.popleft()
        else:
            break

    if player.voice_client.is_playing() or player.voice_client.is_paused():
        player.voice_client.stop()
    else:
        await _play_next(ctx.guild.id, player)

    idle_checker.reset_timer(ctx.guild.id)
    await safe_send(ctx, content=f"⏭️ 已跳過 {count} 首歌曲")

@bot.command(name='loop')
@ensure_player()
async def toggle_loop(ctx: commands.Context, mode: Optional[str] = None):
    """切換或設定單曲循環播放模式"""
    player = music.get_player(ctx.guild.id)
    if mode is None:
        player.loop = not player.loop
    else:
        if mode.lower() in ('on', 'enable', 'true'):
            player.loop = True
        elif mode.lower() in ('off', 'disable', 'false'):
            player.loop = False
        else:
            return await safe_send(ctx, content="❌ 請使用 on 或 off 來設定單曲循環")
    if player.loop:
        await safe_send(ctx, content="🔁 單曲循環已啟用")
    else:
        await safe_send(ctx, content="⏹ 單曲循環已停用")

@bot.command(name='help')
async def custom_help(ctx: commands.Context):
    """顯示幫助訊息"""
    embed = discord.Embed(
        title="🎵 音樂蠑螈指令說明",
        color=0xFFC7EA
    )

    basic_commands = (
        "```\n"
        "!join - 加入語音頻道\n"
        "!leave - 離開頻道並清空播放隊列\n"
        "!clean - 清空播放隊列\n"
        "!help - 顯示此幫助訊息\n"
        "```"
    )

    control_commands = (
        "```\n"
        "!play <連結/搜尋關鍵字> - 串流播放音樂或加入隊列\n"
        "!insert <連結/關鍵字> - 插入音樂到隊列的下一首\n"
        "!skip [數字] - 跳過歌曲\n"
        "!list [頁數] - 顯示播放隊列\n"
        "!now - 顯示當前播放資訊\n"
        "!volume <0-200> - 調整或顯示音量\n"
        "!pause - 暫停/恢復播放\n"
        "!loop [on/off] - 切換單曲循環播放模式\n"
        "!move <from> <to> - 移動播放隊列中的歌曲\n"
        "!del <序號> - 刪除隊列中的指定歌曲\n"
        "```"
    )

    embed.add_field(name="基本指令", value=basic_commands, inline=False)
    embed.add_field(name="播放控制指令", value=control_commands, inline=False)
    await ctx.send(embed=embed)

@bot.command(name='insert')
@in_voice_channel()
@ensure_player()
async def insert(ctx: commands.Context, *, query: str):
    """插入音樂到隊列中的下一首播放"""
    try:
        player = music.get_player(ctx.guild.id)
        await ensure_voice_connection(ctx, player)

        query, playlist_offset = parse_playlist_offset(query)
        async with ctx.typing():
            sources = await YTDLSource.from_url(query, playlist_offset=playlist_offset)
            if not sources:
                return await safe_send(ctx, content="❌ 未找到內容")
            sources, truncated, available = adjust_sources(sources, len(player.queue))
            for source in reversed(sources):
                player.queue.appendleft(source)

            if not player.voice_client.is_playing() and not player.voice_client.is_paused():
                await _play_next(ctx.guild.id, player)
            else:
                schedule_preload(player)

        idle_checker.reset_timer(ctx.guild.id)

        response = f"✅ 已插入 {len(sources)} 首曲目到隊列的下一首"
        if truncated:
            response += f"\n⚠️ 播放清單超過允許數量，僅加入 {available} 首歌曲"
        await safe_send(ctx, content=response)
    except InvalidURL as e:
        await safe_send(ctx, content=f"❌ 無效的 URL: {e}")
    except QueueFull as e:
        await safe_send(ctx, content=f"❌ {e}")
    except Exception as e:
        logger.error(f"插入命令錯誤: {e}")
        await safe_send(ctx, content=f"❌ 插入失敗: {e}")

@bot.command(name='del')
@ensure_player()
async def delete_song(ctx: commands.Context, index: str):
    """刪除播放隊列中的歌曲"""
    try:
        player = music.get_player(ctx.guild.id)
        if not player.queue:
            return await safe_send(ctx, content="📪 播放隊列是空的")
        try:
            idx = int(index)
        except ValueError:
            return await safe_send(ctx, content="❌ 請輸入有效的序列號")
        if idx < 1:
            return await safe_send(ctx, content="❌ 序列號必須大於 0")
        
        lst = list(player.queue)
        if idx > len(lst):
            return await safe_send(ctx, content=f"❌ 序列號超出範圍 (當前隊列長度: {len(lst)})")
        
        song = lst.pop(idx - 1)
        player.queue = deque(lst, maxlen=MAX_QUEUE_LENGTH)
        
        if idx == 1 and player._voice_client and player._voice_client.is_playing():
            schedule_preload(player)
            
        await safe_send(ctx, content=f"✅ 已刪除 **{song.title}** 從播放隊列")
    except Exception as e:
        logger.error(f"刪除命令錯誤: {e}", exc_info=True)
        await safe_send(ctx, content=f"❌ 刪除失敗: {e}")

@bot.command(name='clean')
@ensure_player()
async def clean_queue(ctx: commands.Context):
    """清空播放隊列"""
    try:
        player = music.get_player(ctx.guild.id)
        player.queue.clear()
        await safe_send(ctx, content="🧹 已清空播放隊列")
    except Exception as e:
        logger.error(f"清空隊列失敗: {e}")
        await safe_send(ctx, content=f"❌ 清空失敗: {e}")

@bot.command(name='move')
@ensure_player()
async def move_song(ctx: commands.Context, from_index: str, to_index: str):
    """移動播放隊列中的歌曲"""
    try:
        player = music.get_player(ctx.guild.id)
        if not player.queue:
            return await safe_send(ctx, content="📪 播放隊列是空的")
        
        try:
            from_idx = int(from_index)
            to_idx = int(to_index)
        except ValueError:
            return await safe_send(ctx, content="❌ 請輸入有效的序列號")
        
        if from_idx < 1 or to_idx < 1:
            return await safe_send(ctx, content="❌ 序列號必須大於 0")
        
        lst = list(player.queue)
        if from_idx > len(lst) or to_idx > len(lst):
            return await safe_send(ctx, content=f"❌ 序列號超出範圍 (當前隊列長度: {len(lst)})")
        
        song = lst.pop(from_idx - 1)
        lst.insert(to_idx - 1, song)
        
        player.queue = deque(lst, maxlen=MAX_QUEUE_LENGTH)
        
        if (from_idx == 1 or to_idx == 1) and player._voice_client and player._voice_client.is_playing():
            schedule_preload(player)

        await safe_send(ctx, content=f"✅ 已將 **{song.title}** 從位置 {from_idx} 移動到位置 {to_idx}")
    except Exception as e:
        logger.error(f"移動命令錯誤: {e}")
        await safe_send(ctx, content=f"❌ 移動失敗: {e}")

# 閒置檢測
class IdleChecker:
    """管理語音頻道的閒置檢測器"""
    def __init__(self):
        self.idle_timers = {}
        self.IDLE_TIMEOUT = IDLE_TIMEOUT_SECONDS
        self.CHECK_INTERVAL = CHECK_INTERVAL_SECONDS
        
    def _get_player(self, guild_id):
        """安全地獲取 player 實例"""
        return music.get_player(guild_id)
    
    async def _idle_check_task(self, guild_id):
        """檢查閒置狀態的背景任務"""
        guild_ref = weakref.ref(bot.get_guild(guild_id))
        
        try:
            while True:
                await asyncio.sleep(self.CHECK_INTERVAL)
                
                guild = guild_ref()
                player = self._get_player(guild_id)
                
                if not guild or not player or not player._voice_client:
                    break
                
                # 如果正在播放，不計為閒置
                if player._voice_client.is_playing() or player._voice_client.is_paused():
                    self.reset_timer(guild_id)
                    continue

                last_activity_time = self.idle_timers.get(guild_id, {}).get('last_activity', datetime.datetime.now())
                
                idle_duration = (datetime.datetime.now() - last_activity_time).total_seconds()
                
                if idle_duration >= self.IDLE_TIMEOUT:
                    logger.info(f"伺服器 {guild.name} 閒置超過 {self.IDLE_TIMEOUT} 秒，離開語音頻道")
                    
                    text_channel = player.text_channel
                    
                    try:
                        if player._voice_client:
                            await player._voice_client.disconnect()
                        player.cleanup()
                        if text_channel:
                            await safe_send(text_channel, content=f"⏰ 已閒置 {int(idle_duration)} 秒，自動離開語音頻道")
                    except Exception as e:
                        safe_log_error(guild_id, "idle_timeout", f"閒置超時處理出錯: {e}")
                    
                    self.idle_timers.pop(guild_id, None)
                    break
        except asyncio.CancelledError:
            self.idle_timers.pop(guild_id, None)
        except Exception as e:
            safe_log_error(guild_id, "idle_check", f"閒置檢查任務發生錯誤: {e}")
            self.idle_timers.pop(guild_id, None)
    
    def reset_timer(self, guild_id):
        """重置指定伺服器的閒置計時器"""
        if guild_id in self.idle_timers:
            self.idle_timers[guild_id]['last_activity'] = datetime.datetime.now()
        else:
            self.idle_timers[guild_id] = {
                'last_activity': datetime.datetime.now(),
                'task': asyncio.create_task(self._idle_check_task(guild_id))
            }
    
    def cancel_timer(self, guild_id):
        """取消指定伺服器的閒置計時器"""
        if guild_id in self.idle_timers:
            if 'task' in self.idle_timers[guild_id]:
                self.idle_timers[guild_id]['task'].cancel()
            self.idle_timers.pop(guild_id, None)

idle_checker = IdleChecker()

@bot.event
async def on_ready():
    print(f'✓ {bot.user} 已成功登入並準備就緒！')
    
    active_voice_clients = list(bot.voice_clients)
    
    if active_voice_clients:
        logger.warning(f"偵測到 {len(active_voice_clients)} 個殘留的語音連接，正在強制斷開...")
        for vc in active_voice_clients:
            try:
                if vc.guild.id in music.players:
                    music.players[vc.guild.id].cleanup()
                    del music.players[vc.guild.id]
                    idle_checker.cancel_timer(vc.guild.id)
                
                await vc.disconnect(force=True)
            except Exception as e:
                logger.error(f"在清理伺服器 {vc.guild.name} 的殘留連接時發生錯誤: {e}")
    
    music.players.clear()

# 錯誤處理
@bot.event
async def on_command_error(ctx, error):
    """全域指令錯誤處理器"""
    error = getattr(error, 'original', error)
    
    custom_messages = {
        commands.CommandNotFound: "❌ 未知指令，使用 !help 查看可用指令",
        commands.MissingRequiredArgument: "❌ 缺少必要參數",
        commands.BadArgument: "❌ 參數格式錯誤",
        asyncio.TimeoutError: "⏳ 操作逾時，請稍後再試",
        discord.HTTPException: "🌐 網路連線不穩定，請重試",
        youtube_dl.DownloadError: "❌ 媒體下載失敗"
    }
    
    if isinstance(error, commands.CheckFailure):
        if str(error) == "用戶未在語音頻道":
            return
        else:
            return await safe_send(ctx, content="❌ 權限不足或條件不符")
    
    for error_type, message in custom_messages.items():
        if isinstance(error, error_type):
            return await safe_send(ctx, content=message)
    
    if isinstance(error, MusicError):
        return await safe_send(ctx, content=f"🎵 音樂錯誤: {error}")
    
    guild_id = ctx.guild.id if ctx.guild else 0
    safe_log_error(guild_id, "unhandled_error", f"未處理的錯誤: {type(error)} - {str(error)}")
    await safe_send(ctx, content=f"⚠️ 發生未預期錯誤: {str(error)}")

# 啟動點
if __name__ == "__main__":
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        raise ValueError("未找到 DISCORD_BOT_TOKEN 環境變量")
    
    try:
        bot.run(token, log_level=logging.INFO)
    except KeyboardInterrupt:
        logging.info("收到關閉信號...")
    finally:
        # 優雅地關閉執行緒池
        logging.info("正在關閉 ThreadPoolExecutor...")
        thread_executor.shutdown(wait=True)
        logging.info("Executor 已關閉。")