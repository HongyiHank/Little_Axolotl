# utils/player.py

from collections import deque
from typing import Optional, Deque

import discord
from sources.youtube import YTDLSource
from utils.config import DEFAULT_VOLUME, MAX_QUEUE_LENGTH

class GuildMusicPlayer:
    """管理單一伺服器的音樂播放器狀態"""
    
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

from utils.errors import MusicError