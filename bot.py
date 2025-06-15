# main.py

import asyncio
import logging
import os
import concurrent.futures

import discord
from discord.ext import commands
from dotenv import load_dotenv

from utils.errors import MusicError

# 載入環境變數
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')
if not TOKEN:
    raise ValueError("未找到 DISCORD_BOT_TOKEN 環境變量")

# 設定日誌
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('discord')

# Discord 機器人初始化
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True # 語音狀態是必要的

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    help_command=None,
    case_insensitive=True # 從 config 檔案讀取設定值
)

# 將執行緒池附加到 bot 實例上，以便在 Cog 中存取
bot.thread_executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)

# 全域錯誤處理器
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """全域指令錯誤處理器"""
    original_error = getattr(error, 'original', error)

    if isinstance(original_error, commands.CommandNotFound):
        return # 可以選擇靜默處理或發送訊息
    
    if isinstance(original_error, commands.CheckFailure):
        await ctx.send("❌ 您不符合執行此指令的條件 (例如：不在語音頻道中)。", ephemeral=True)
        return

    if isinstance(original_error, MusicError):
        await ctx.send(f"🎵 音樂錯誤: {original_error}", ephemeral=True)
        return
    
    # 其他常見錯誤
    if isinstance(original_error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.send(f"❌ 指令參數錯誤: {original_error}", ephemeral=True)
        return

    # 未處理的錯誤記錄到日誌
    logger.error(f"在指令 {ctx.command} 中發生未處理的錯誤:", exc_info=original_error)
    await ctx.send(f"⚠️ 發生未預期的錯誤，請聯絡管理員。")


@bot.event
async def on_ready():
    """當機器人準備就緒時"""
    logger.info(f'✓ {bot.user} 已成功登入並準備就緒！')
    logger.info(f"在 {len(bot.guilds)} 個伺服器中運行")
    
    # 清理殘留的語音連接 (可選，但建議)
    for vc in bot.voice_clients:
        await vc.disconnect(force=True)
        logger.warning(f"已強制斷開在 {vc.guild.name} 的殘留連接")


async def load_extensions():
    """載入所有 cogs"""
    for filename in os.listdir('./cogs'):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'cogs.{filename[:-3]}')
                logger.info(f"已成功載入 Cog: {filename}")
            except Exception as e:
                logger.error(f"載入 Cog {filename} 失敗。", exc_info=e)


async def main():
    async with bot:
        await load_extensions()
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到關閉信號...")
    finally:
        logger.info("正在關閉 ThreadPoolExecutor...")
        bot.thread_executor.shutdown(wait=True)
        logger.info("Executor 已關閉。")