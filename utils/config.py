# utils/config.py

import os
from typing import Any

# 載入環境變數
from dotenv import load_dotenv
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