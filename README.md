# 🎵 小蠑螈 - 強大的Discord 機器人

![小蠑螈-端午節特別版](/icon/dragon_boat_festival_version.png)
使用Google Imagen 3 以及 Gemini 2.0 Flash 生成

## ✨ 特色

- 支援Youtube 以及 Youtube Music
- 支援搜尋
- 支援播放清單、YT Music電台
- 強大的指令系統

## 🛠️ 安裝與使用

### 1. 安裝依賴
`pip install -r requirements.txt`

### 2. 安裝ffmpeg
[ffmpeg 官網](https://ffmpeg.org/download.html)

### 3. 設定環境變數
設定 .env 檔案
- DISCORD_BOT_TOKEN
- 其它選項若您不清楚，請留空

### 4. 啟動機器人
`python bot.py`

### 5. enjoy

## 📜 指令說明
| 指令 | 功能 | 範例 |
|------|------|------|
| `!join` | 連入語音頻道 | - |
| `!leave` | 離開語音頻道 | - |
| `!clean` | 清空播放隊列 | - |
| `!help` | 顯示幫助訊息 | - |
| `!play` | 播放音樂 | `!play 米津玄師 Lemon` |
| `!insert` | 插入歌曲 | `!insert 米津玄師 Loser` |
| `!list` | 顯示隊列 <頁數>(默認為一) | `!list 2` |
| `!skip` | 跳過歌曲 <數量>(默認為一) | `!skip 3` |
| `!now` | 顯示當前播放資訊 | - |
| `!volume` | 調整音量 <0-200> | `!volume 50` |
| `!pause` | 暫停/繼續播放 | - |
| `!loop` | 切換單曲循環播放模式 | - |
| `!move <from> <to>` | 移動播放隊列中的歌曲 | `!move 1 3` |
| `!del <序號>` | 刪除隊列中的指定歌曲 | `!del 2` |

## 🚀 未來開發計劃

開發進度：僅為想法：💭｜開發中：🔨｜測試中：🧪

### 🎵 音樂功能
- [🔨] 支援多平台音源（Spotify, bilibili 等其它平台）

### 🤖 AI 對話
- [🔨] 引入Gemini 2.5 Flash (經費有限，每天用量有限)
- [🔨] AI 自然語言交互(如：幫我播放一首米津玄師的歌曲)

### 🎭 其它、雜項
- [🔨] 添加英文翻譯、readme 文件
- [🔨] 模組化管理
- [💭] 透過 Discord 直播播放影片
> 已完成之功能或特性請移步至 [release](https://github.com/HongyiHank/Little_Axolotl/releases) 頁面查看更新說明

## 📝 貢獻

> 歡迎提交 Pull Request，提交過後您的大名將會出現在貢獻者清單中<br>
> Pull Request 請提交到`dev`分支

## 📄 授權
[MIT License](LICENSE)
