# cogs/music.py

import asyncio
import datetime
import logging
import re
import time
import weakref
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import discord
from discord import ui
from discord.ext import commands

# 從我們的模組導入
from utils.config import (
    MAX_QUEUE_LENGTH, QUEUE_PAGE_SIZE, BUTTON_TIMEOUT_SECONDS,
    IDLE_TIMEOUT_SECONDS, CHECK_INTERVAL_SECONDS
)
from utils.errors import MusicError, NetworkError, InvalidURL, QueueFull
from utils.player import GuildMusicPlayer
from sources.youtube import YTDLSource, create_ffmpeg_audio, adjust_sources

logger = logging.getLogger('discord.little_axolotl.music')

# 輔助函數
async def safe_send(ctx: commands.Context, content: str = None, **kwargs) -> Optional[discord.Message]:
    """安全地發送訊息，捕捉並記錄 HTTP 錯誤"""
    try:
        # 如果是互動，則使用 interaction.followup.send
        if isinstance(ctx, discord.Interaction):
            return await ctx.followup.send(content, **kwargs)
        return await ctx.send(content, **kwargs)
    except discord.HTTPException as e:
        logger.error(f"訊息發送失敗：{e}")
        return None

def parse_playlist_offset(query: str) -> Tuple[str, int]:
    """從查詢字串中解析播放清單偏移量 (例如 '... -5')"""
    from utils.config import MAX_PLAYLIST
    playlist_offset = 0
    offset_pattern = re.search(r'\s+\-(\d+)$', query)
    if offset_pattern:
        page_number = int(offset_pattern.group(1))
        if page_number >= 1:
            playlist_offset = (page_number - 1) * MAX_PLAYLIST
            query = query[:offset_pattern.start()].strip()
    return query, playlist_offset
    
def _format_time(seconds: float) -> str:
    """格式化時間顯示"""
    seconds = int(seconds)
    if seconds < 0: return "未知"
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

def _create_queue_embed(player: GuildMusicPlayer, page: int) -> Tuple[Optional[discord.Embed], int]:
    """建立播放隊列頁面的 Embed"""
    if not player.queue:
        embed = discord.Embed(description="📪 播放隊列是空的", color=0xFFC7EA)
        return embed, 1

    total_pages = (len(player.queue) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE
    page = max(1, min(page, total_pages))
    start = (page - 1) * QUEUE_PAGE_SIZE
    end = start + QUEUE_PAGE_SIZE
    queue_slice = list(player.queue)[start:end]

    queue_list = [
        f"`{idx}.` [**{item.title}**]({item.webpage_url}) `({_format_time(item.duration)})`"
        for idx, item in enumerate(queue_slice, start=start + 1)
    ]

    embed = discord.Embed(
        title=f"🎵 播放隊列 (第 {page}/{total_pages} 頁)",
        description="\n".join(queue_list) or "沒有內容",
        color=0xFFC7EA
    )
    embed.set_footer(text=f"共 {len(player.queue)} 首歌曲")
    return embed, total_pages

# 主 Cog 類別
class Music(commands.Cog):
    """音樂播放相關的指令與功能"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildMusicPlayer] = {}
        self.idle_checker = IdleChecker(self)
        self.logger = logger

    def cog_unload(self):
        """Cog 卸載時的清理工作"""
        self.idle_checker.cancel_all_timers()
        self.bot.loop.create_task(self.cleanup_all_players())

    async def cleanup_all_players(self):
        """非同步清理所有播放器"""
        for player in self.players.values():
            if player._voice_client:
                await player._voice_client.disconnect(force=True)
            player.cleanup()
        self.players.clear()

    # Cog 內部檢查器
    async def cog_check(self, ctx: commands.Context) -> bool:
        """所有指令執行前的檢查"""
        if not ctx.guild:
            await ctx.send("❌ 此指令僅能在伺服器中使用")
            return False
        return True

    async def cog_before_invoke(self, ctx: commands.Context):
        """確保在執行指令前，伺服器播放器已存在"""
        self.get_player(ctx.guild.id)
        
    def in_voice_channel():
        """檢查指令使用者是否在語音頻道中 (作為裝飾器使用)"""
        async def predicate(ctx: commands.Context) -> bool:
            if not ctx.author.voice:
                await ctx.send("❌ 請先加入語音頻道後再使用此指令！")
                raise commands.CheckFailure("用戶未在語音頻道")
            return True
        return commands.check(predicate)

    # 輔助方法
    def get_player(self, guild_id: int) -> GuildMusicPlayer:
        return self.players.setdefault(guild_id, GuildMusicPlayer())

    async def _ensure_voice_connection(self, ctx: commands.Context) -> GuildMusicPlayer:
        player = self.get_player(ctx.guild.id)
        if not player._voice_client or not player._voice_client.is_connected():
            player.cleanup()
            user_channel = ctx.author.voice.channel
            player.voice_client = await user_channel.connect(timeout=60.0, reconnect=True)
            player.text_channel = ctx.channel
            self.idle_checker.reset_timer(ctx.guild.id)
            await safe_send(ctx, content=f"✅ 已加入語音頻道 **{user_channel}**")
        elif player._voice_client.channel != ctx.author.voice.channel:
            await player._voice_client.move_to(ctx.author.voice.channel)
            await safe_send(ctx, content=f"✅ 已移動至 **{ctx.author.voice.channel}** 頻道")
        return player
    
    # 播放邏輯
    def _setup_after(self, guild_id: int) -> Callable:
        def after_callback(error: Optional[Exception]) -> None:
            self.bot.loop.call_soon_threadsafe(
                asyncio.create_task,
                self._after_playing_callback(guild_id, error)
            )
        return after_callback

    async def _after_playing_callback(self, guild_id: int, error: Optional[Exception]):
        player = self.get_player(guild_id)
        if not player._voice_client or not player._voice_client.is_connected():
            return
        if error:
            self.logger.error(f"播放錯誤 (Guild {guild_id}): {error}", exc_info=True)
            if player.text_channel:
                await safe_send(player.text_channel, content=f"⚠️ 播放錯誤: `{error}`")
        
        if player.loop and player.now_playing:
            return await self._replay_current(player)
        
        if player.loop_queue and player.now_playing:
            player.queue.append(player.now_playing)
            
        await self._play_next(guild_id)
        
    async def _replay_current(self, player: GuildMusicPlayer):
        guild_id = player.voice_client.guild.id
        try:
            await player.now_playing.prepare(loop=self.bot.loop, executor=self.bot.thread_executor)
            new_source = create_ffmpeg_audio(player.now_playing.url)
            replayed_source = YTDLSource(new_source, data=player.now_playing.data, volume=player.volume, lazy=False)
            player.now_playing = replayed_source
            player.voice_client.play(player.now_playing, after=self._setup_after(guild_id))
            player.start_time = time.time()
            player.media_offset = 0.0
            self.logger.debug(f"單曲循環：重播 {player.now_playing.title}")
        except Exception as e:
            self.logger.error(f"重播失敗 (Guild {guild_id}): {e}")
            if player.text_channel:
                await safe_send(player.text_channel, content=f"⚠️ 重播失敗: {e}")
            await self._play_next(guild_id)

    async def _schedule_preload(self, player: GuildMusicPlayer):
        if player.queue and player._voice_client and player._voice_client.is_connected():
            next_track = player.queue[0]
            if next_track.lazy and not next_track._is_prepared:
                guild_id = player._voice_client.guild.id
                asyncio.create_task(self._preload_track(next_track, guild_id))

    async def _preload_track(self, track: YTDLSource, guild_id: int):
        try:
            await track.prepare(loop=self.bot.loop, executor=self.bot.thread_executor)
            self.logger.info(f"✓ [預加載成功] Guild {guild_id}: {track.title}")
        except Exception as e:
            self.logger.warning(f"預加載失敗 (Guild {guild_id}): {track.title} - {e}")
            
    async def _play_next(self, guild_id: int):
        player = self.get_player(guild_id)
        if not player._voice_client or not player._voice_client.is_connected() or player.voice_client.is_playing():
            return
            
        while player.queue:
            next_track = player.queue.popleft()
            try:
                await next_track.prepare(loop=self.bot.loop, executor=self.bot.thread_executor)
                if not next_track.url:
                    raise InvalidURL("音頻源 URL 無效或已過期")
                
                new_audio_source = create_ffmpeg_audio(next_track.url)
                next_track.original = new_audio_source
                next_track.source = new_audio_source
                next_track.volume = player.volume
                
                player.now_playing = next_track
                player.start_time = time.time()
                player.media_offset = 0.0
                
                player.voice_client.play(next_track, after=self._setup_after(guild_id))
                
                if player.text_channel:
                    await self.display_now_playing_info(player.text_channel, player)
                
                self.idle_checker.reset_timer(guild_id)
                self.logger.debug(f"開始播放 (Guild {guild_id}): {next_track.title}")
                
                await self._schedule_preload(player)
                return

            except Exception as e:
                self.logger.error(f"音軌準備失敗 (Guild {guild_id}): {e}", exc_info=True)
                if player.text_channel:
                    await safe_send(player.text_channel, content=f"⚠️ 跳過錯誤歌曲: **{next_track.title}** (`{e}`)")
                continue

        player.now_playing = None
        player.start_time = None
        if player.text_channel:
            await safe_send(player.text_channel, content="✅ 播放完畢")
        self.logger.info(f"伺服器 {guild_id} 播放隊列已完成")

    async def display_now_playing_info(self, ctx_or_channel, player: GuildMusicPlayer, embed_only=False):
        if not player.now_playing:
            if not embed_only:
                await safe_send(ctx_or_channel, content="❌ 目前沒有正在播放的歌曲")
            return None

        is_paused = player.voice_client.is_paused()
        current_media_time = player.media_offset if is_paused else player.media_offset + (time.time() - player.start_time if player.start_time else 0)
        duration = player.now_playing.duration or 0
        current_media_time = min(current_media_time, duration) if duration > 0 else current_media_time

        embed = discord.Embed(
            title=f"🎵 {'暫停中' if is_paused else '正在播放'}",
            description=f"### [{player.now_playing.title}]({player.now_playing.webpage_url})",
            color=0xAAAAAA if is_paused else 0xFFC7EA
        )
        if player.now_playing.data.get('thumbnail'):
            embed.set_thumbnail(url=player.now_playing.data['thumbnail'])
            
        embed.add_field(name="👤 上傳者", value=f"```{player.now_playing.data.get('uploader', '未知')}```", inline=True)
        embed.add_field(name="🔊 音量", value=f"```{int(player.volume * 100)}%```", inline=True)
        
        embed.add_field(
            name="⏱️ 播放進度",
            value=f"`{_format_time(current_media_time)} / {_format_time(duration)}`\n{_build_progress_bar(int(current_media_time), int(duration))}",
            inline=False
        )

        if player.queue:
            embed.add_field(name="🎵 下一首", value=f"```{player.queue[0].title}```", inline=False)
        
        embed.set_footer(text=f"隊列中還有 {len(player.queue)} 首歌曲")
        
        if embed_only:
            return embed
        
        view = PlayerControlsView(self)
        
        try:
            if isinstance(ctx_or_channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread, commands.Context)):
                await ctx_or_channel.send(embed=embed, view=view)
        except Exception as e:
            self.logger.error(f"發送 'Now Playing' 訊息失敗: {e}")

    # 事件監聽器
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot and member.id == self.bot.user.id:
            if before.channel and not after.channel:
                player = self.players.pop(member.guild.id, None)
                if player:
                    player.cleanup()
                    self.idle_checker.cancel_timer(member.guild.id)
                    self.logger.info(f"已清空 {member.guild.name} 的播放隊列，因為機器人已退出語音頻道")
            return

        if not before.channel:
            return

        bot_vc = member.guild.me.voice
        if not bot_vc or bot_vc.channel.id != before.channel.id:
            return

        # 檢查頻道中是否只剩下機器人
        if sum(1 for m in before.channel.members if not m.bot) == 0:
            player = self.get_player(member.guild.id)
            self.logger.info(f"所有用戶已離開語音頻道 {before.channel.name}，機器人自動退出")
            if player.text_channel:
                await safe_send(player.text_channel, "👋 所有用戶已離開語音頻道，機器人自動退出")
            if player._voice_client:
                await player._voice_client.disconnect()
            player.cleanup()
            self.players.pop(member.guild.id, None)
            self.idle_checker.cancel_timer(member.guild.id)

    # 指令
    @commands.command(name='join', help="加入使用者所在的語音頻道")
    @in_voice_channel()
    async def join(self, ctx: commands.Context):
        await self._ensure_voice_connection(ctx)

    @commands.command(name='leave', help="離開語音頻道並清空播放隊列")
    async def leave(self, ctx: commands.Context):
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        player = self.players.pop(ctx.guild.id, None)
        if player:
            player.cleanup()
        self.idle_checker.cancel_timer(ctx.guild.id)
        await safe_send(ctx, content="👋 已離開語音頻道並清空播放隊列")

    @commands.command(name='play', aliases=['p'], help="播放音樂或加入播放隊列")
    @in_voice_channel()
    async def play(self, ctx: commands.Context, *, query: str):
        player = await self._ensure_voice_connection(ctx)
        
        if len(player.queue) >= MAX_QUEUE_LENGTH:
            raise QueueFull()

        clean_query, playlist_offset = parse_playlist_offset(query)
        
        async with ctx.typing():
            try:
                sources = await YTDLSource.from_url(
                    clean_query, 
                    loop=self.bot.loop, 
                    executor=self.bot.thread_executor, 
                    playlist_offset=playlist_offset
                )
            except (InvalidURL, NetworkError) as e:
                return await safe_send(ctx, content=f"❌ 錯誤：{e}")
                
            if not sources:
                return await safe_send(ctx, content="❌ 未找到任何音樂內容")

            adjusted_sources, truncated, avail = adjust_sources(sources, len(player.queue))
            player.queue.extend(adjusted_sources)

            if len(adjusted_sources) > 1:
                response = f"✅ 已加入 {len(adjusted_sources)} 首歌曲到播放隊列"
            else:
                response = f"✅ 已加入歌曲：**{adjusted_sources[0].title}**（隊列位置：{len(player.queue)}）"
            
            if truncated:
                response += f"\n⚠️ 播放清單超過容量限制，僅加入前 {avail} 首歌曲"
            
            await safe_send(ctx, content=response)

            if not player.voice_client.is_playing() and not player.voice_client.is_paused():
                await self._play_next(ctx.guild.id)
            elif len(player.queue) == len(adjusted_sources):
                await self._schedule_preload(player)
        
        self.idle_checker.reset_timer(ctx.guild.id)

    @commands.command(name='list', aliases=['q'], help="顯示播放隊列")
    async def list_queue(self, ctx: commands.Context, page: int = 1):
        player = self.get_player(ctx.guild.id)
        embed, total_pages = _create_queue_embed(player, page)
        view = QueueView(self, page, total_pages) if total_pages > 1 or player.queue else None
        await safe_send(ctx, embed=embed, view=view)

    @commands.command(name='now', aliases=['np'], help="顯示當前播放資訊")
    async def now_playing(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if player.now_playing:
            await self.display_now_playing_info(ctx, player)
        else:
            await safe_send(ctx, content="❌ 目前沒有正在播放的歌曲")

    @commands.command(name='pause', help="暫停/恢復播放")
    async def toggle_pause(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        if not player._voice_client or not player.now_playing:
            return await safe_send(ctx, content="❌ 目前沒有正在播放的歌曲")

        if player.voice_client.is_paused():
            player.voice_client.resume()
            if player.start_time is None: player.start_time = time.time()
            self.idle_checker.reset_timer(ctx.guild.id)
            await safe_send(ctx, content="▶ 已恢復播放")
        else:
            player.voice_client.pause()
            if player.start_time is not None:
                player.media_offset += time.time() - player.start_time
                player.start_time = None
            await safe_send(ctx, content="⏸ 已暫停播放")

    @commands.command(name='skip', aliases=['s'], help="跳過歌曲")
    async def skip(self, ctx: commands.Context, skip_count: str = "1"):
        try:
            count = int(skip_count)
            if count < 1: raise ValueError
        except ValueError:
            return await safe_send(ctx, content="❌ 請輸入一個大於0的數字")

        player = self.get_player(ctx.guild.id)
        if not player.now_playing and not player.queue:
            return await safe_send(ctx, content="❌ 播放隊列是空的")

        if count > 1 and player.queue:
            actual_skipped = min(count - 1, len(player.queue))
            for _ in range(actual_skipped):
                player.queue.popleft()
        
        if player.voice_client.is_playing() or player.voice_client.is_paused():
            player.voice_client.stop()
        else: # 如果因為某種原因停止了但隊列仍在
            await self._play_next(ctx.guild.id)
            
        self.idle_checker.reset_timer(ctx.guild.id)
        await safe_send(ctx, content=f"⏭️ 已跳過 {count} 首歌曲")

    @commands.command(name='volume', aliases=['vol'], help="調整或顯示音量 (0-200)")
    async def set_volume(self, ctx: commands.Context, volume: Optional[int] = None):
        player = self.get_player(ctx.guild.id)
        if volume is None:
            return await safe_send(ctx, content=f"🔊 當前音量為 {int(player.volume * 100)}%")
        
        if not 0 <= volume <= 200:
            return await safe_send(ctx, content="❌ 音量必須在 0 到 200 之間")

        player.volume = volume / 100.0
        if player.now_playing and player._voice_client and player._voice_client.source:
            player._voice_client.source.volume = player.volume

        await safe_send(ctx, content=f"🔊 音量已設定為 {volume}%")

    @commands.command(name='loop', help="切換單曲循環模式 (on/off)")
    async def toggle_loop(self, ctx: commands.Context, mode: Optional[str] = None):
        player = self.get_player(ctx.guild.id)
        if mode is None:
            player.loop = not player.loop
        else:
            mode = mode.lower()
            if mode in ('on', 'true', 'enable'): player.loop = True
            elif mode in ('off', 'false', 'disable'): player.loop = False
            else: return await safe_send(ctx, "❌ 無效模式，請使用 on 或 off")
        await safe_send(ctx, content=f"🔁 單曲循環已{'啟用' if player.loop else '停用'}")

    @commands.command(name='clean', help="清空播放隊列")
    async def clean_queue(self, ctx: commands.Context):
        player = self.get_player(ctx.guild.id)
        player.queue.clear()
        await safe_send(ctx, content="🧹 已清空播放隊列")

    @commands.command(name='del', help="刪除隊列中指定的歌曲")
    async def delete_song(self, ctx: commands.Context, index: int):
        player = self.get_player(ctx.guild.id)
        if not player.queue: return await safe_send(ctx, content="📪 播放隊列是空的")
        if not 1 <= index <= len(player.queue):
            return await safe_send(ctx, f"❌ 序列號超出範圍 (1-{len(player.queue)})")
        
        lst = list(player.queue)
        song = lst.pop(index - 1)
        player.queue = deque(lst, maxlen=MAX_QUEUE_LENGTH)
        
        if index == 1 and player._voice_client and player._voice_client.is_playing():
            await self._schedule_preload(player)
            
        await safe_send(ctx, content=f"✅ 已從隊列刪除 **{song.title}**")

    @commands.command(name='move', help="移動隊列中的歌曲")
    async def move_song(self, ctx: commands.Context, from_pos: int, to_pos: int):
        player = self.get_player(ctx.guild.id)
        if not player.queue: return await safe_send(ctx, content="📪 播放隊列是空的")
        
        q_len = len(player.queue)
        if not (1 <= from_pos <= q_len and 1 <= to_pos <= q_len):
            return await safe_send(ctx, f"❌ 序列號超出範圍 (1-{q_len})")
        
        lst = list(player.queue)
        song = lst.pop(from_pos - 1)
        lst.insert(to_pos - 1, song)
        player.queue = deque(lst, maxlen=MAX_QUEUE_LENGTH)
        
        if (from_pos == 1 or to_pos == 1) and player._voice_client and player._voice_client.is_playing():
            await self._schedule_preload(player)
            
        await safe_send(ctx, content=f"✅ 已將 **{song.title}** 從位置 {from_pos} 移動到 {to_pos}")

    @commands.command(name='insert', help="插入音樂到隊列下一首")
    @in_voice_channel()
    async def insert(self, ctx: commands.Context, *, query: str):
        player = await self._ensure_voice_connection(ctx)
        
        async with ctx.typing():
            sources = await YTDLSource.from_url(query, loop=self.bot.loop, executor=self.bot.thread_executor)
            if not sources: return await safe_send(ctx, "❌ 未找到內容")
            
            sources, truncated, avail = adjust_sources(sources, len(player.queue))
            for source in reversed(sources):
                player.queue.appendleft(source)
            
            response = f"✅ 已插入 {len(sources)} 首曲目到隊列頂部"
            if truncated: response += f"\n⚠️ 播放清單超過容量，僅加入 {avail} 首"
            await safe_send(ctx, response)

            if not player.voice_client.is_playing():
                await self._play_next(ctx.guild.id)
            else:
                await self._schedule_preload(player)

    @commands.command(name='help', help="顯示幫助訊息")
    async def custom_help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎵 小蠑螈 - 音樂指令說明",
            description="使用 `!` 作為指令前綴",
            color=0xFFC7EA
        )
        commands_list = {
            "基本指令": {
                "join": "加入語音頻道",
                "leave": "離開頻道並清空播放隊列",
                "clean": "清空播放隊列",
                "help": "顯示此幫助訊息",
            },
            "播放控制": {
                "play <連結/關鍵字>": "播放音樂或加入隊列",
                "pause": "暫停/恢復播放",
                "skip [數字]": "跳過歌曲",
                "now": "顯示當前播放資訊",
                "list [頁數]": "顯示播放隊列",
                "volume <0-200>": "調整或顯示音量",
                "loop [on/off]": "切換單曲循環模式",
            },
            "隊列管理": {
                "insert <連結/關鍵字>": "插入音樂到隊列的下一首",
                "move <from> <to>": "移動播放隊列中的歌曲",
                "del <序號>": "刪除隊列中的指定歌曲",
            }
        }
        for category, cmds in commands_list.items():
            embed.add_field(
                name=category,
                value="```\n" + "\n".join([f"{name:<22} - {desc}" for name, desc in cmds.items()]) + "```",
                inline=False
            )
        await ctx.send(embed=embed)


# UI 視圖類別
class QueueView(ui.View):
    def __init__(self, cog: Music, current_page: int, total_pages: int):
        super().__init__(timeout=BUTTON_TIMEOUT_SECONDS)
        self.cog = cog
        self.current_page = current_page
        self.total_pages = total_pages
        self.update_buttons()

    def update_buttons(self):
        self.children[0].disabled = self.current_page <= 1
        self.children[1].disabled = self.current_page >= self.total_pages

    async def _update_message(self, interaction: discord.Interaction, new_page: int):
        player = self.cog.get_player(interaction.guild_id)
        embed, new_total_pages = _create_queue_embed(player, new_page)
        self.current_page = max(1, min(new_page, new_total_pages))
        self.total_pages = new_total_pages
        new_view = QueueView(self.cog, self.current_page, self.total_pages)
        await interaction.response.edit_message(embed=embed, view=new_view)

    @ui.button(label="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page - 1)

    @ui.button(label="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page + 1)

    @ui.button(label="🔄", style=discord.ButtonStyle.primary)
    async def refresh_queue(self, interaction: discord.Interaction, button: ui.Button):
        await self._update_message(interaction, self.current_page)


class PlayerControlsView(ui.View):
    def __init__(self, cog: Music):
        super().__init__(timeout=None) # 控制按鈕永不超時
        self.cog = cog

    async def _execute_command(self, interaction: discord.Interaction, command_name: str, *args):
        """模擬執行一個指令"""
        ctx = await self.cog.bot.get_context(interaction.message)
        ctx.author = interaction.user # 確保指令執行者是按按鈕的人
        command = self.cog.bot.get_command(command_name)
        if command:
            await ctx.invoke(command, *args)
            await interaction.response.send_message(f"✅ 已執行 `{command_name}`", ephemeral=True, delete_after=3)
        else:
            await interaction.response.send_message("❌ 指令未找到", ephemeral=True, delete_after=3)

    @ui.button(emoji="⏯️", style=discord.ButtonStyle.primary, row=0)
    async def toggle_playback(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self._execute_command(interaction, 'pause')
        
    @ui.button(emoji="⏭️", style=discord.ButtonStyle.primary, row=0)
    async def skip_track(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self._execute_command(interaction, 'skip')

    @ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_loop(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self._execute_command(interaction, 'loop')

    @ui.button(emoji="⏹️", style=discord.ButtonStyle.secondary, row=0)
    async def stop_player(self, interaction: discord.Interaction, button: ui.Button):
        """清空隊列並停止播放"""
        await interaction.response.defer()
        player = self.cog.get_player(interaction.guild_id)
        player.queue.clear()
        if player._voice_client:
            player._voice_client.stop()
        await safe_send(interaction, "⏹️ 已停止播放並清空隊列", ephemeral=True)
        
    @ui.button(emoji="🚪", style=discord.ButtonStyle.danger, row=0)
    async def leave_channel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer()
        await self._execute_command(interaction, 'leave')


# 閒置檢測類別
class IdleChecker:
    def __init__(self, cog: Music):
        self.cog = cog
        self.idle_timers: Dict[int, Dict[str, Any]] = {}

    def reset_timer(self, guild_id: int):
        if guild_id in self.idle_timers:
            self.idle_timers[guild_id]['last_activity'] = datetime.datetime.now(datetime.timezone.utc)
        else:
            task = self.cog.bot.loop.create_task(self._idle_check_task(guild_id))
            self.idle_timers[guild_id] = {
                'last_activity': datetime.datetime.now(datetime.timezone.utc),
                'task': task
            }

    def cancel_timer(self, guild_id: int):
        if guild_id in self.idle_timers:
            self.idle_timers[guild_id]['task'].cancel()
            self.idle_timers.pop(guild_id, None)
    
    def cancel_all_timers(self):
        for timer_data in self.idle_timers.values():
            timer_data['task'].cancel()
        self.idle_timers.clear()

    async def _idle_check_task(self, guild_id: int):
        guild_ref = weakref.ref(self.cog.bot.get_guild(guild_id))
        try:
            while True:
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                guild = guild_ref()
                player = self.cog.players.get(guild_id)
                if not guild or not player or not player._voice_client:
                    break
                
                if player._voice_client.is_playing() and not player._voice_client.is_paused():
                    self.reset_timer(guild_id)
                    continue

                last_activity = self.idle_timers.get(guild_id, {}).get('last_activity')
                if not last_activity:
                    break # 計時器被取消
                    
                idle_duration = (datetime.datetime.now(datetime.timezone.utc) - last_activity).total_seconds()
                
                if idle_duration >= IDLE_TIMEOUT_SECONDS:
                    self.cog.logger.info(f"伺服器 {guild.name} 閒置超過 {IDLE_TIMEOUT_SECONDS} 秒")
                    if player.text_channel:
                        await safe_send(player.text_channel, f"⏰ 已閒置超過 {int(IDLE_TIMEOUT_SECONDS/60)} 分鐘，自動離開頻道。")
                    if player._voice_client:
                        await player._voice_client.disconnect()
                    player.cleanup()
                    self.cog.players.pop(guild_id, None)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.idle_timers.pop(guild_id, None)

# Cog 的 setup 函數
async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))