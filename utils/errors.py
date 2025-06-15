# utils/errors.py

from utils.config import MAX_QUEUE_LENGTH

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