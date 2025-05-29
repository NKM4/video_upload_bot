import os
import subprocess
import asyncio
from dotenv import load_dotenv
from pyngrok import ngrok, conf
import uvicorn
import discord
from discord.ext import commands
from app import app as fastapi_app, bot_ref

# ────────────── 起動前処理 ──────────────
subprocess.run(["pkill", "-f", "ngrok"])         # 既存 ngrok プロセスを終了
print("起動開始")

load_dotenv()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
NGROK_PATH = os.getenv("NGROK_PATH")             # /usr/bin/ngrok 等

# ngrok トンネル生成
pyngrok_config = conf.PyngrokConfig(ngrok_path=NGROK_PATH)
FASTAPI_PORT = 8000
public_url = ngrok.connect(FASTAPI_PORT, pyngrok_config=pyngrok_config)
os.environ["TUNNEL_URL"] = public_url.public_url
print(f"ngrok OK: {public_url}")

# Discord Bot 初期化
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
bot_ref.bot = bot                                   # Bot 参照を共有

@bot.event
async def on_ready():
    print(f"BOTログイン成功: {bot.user}")

# ────────────── FastAPI 非同期サーバ ──────────────
async def start_fastapi():
    """
    uvicorn の非同期サーバーを起動し、await で待機
    （uvicorn.run は使わない：内部で asyncio.run が呼ばれるため）
    """
    config = uvicorn.Config(
        fastapi_app,
        host="0.0.0.0",
        port=FASTAPI_PORT,
        loop="asyncio",
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()

# ────────────── エントリポイント ──────────────
async def main():
    print("BOT起動準備中")
    async with bot:
        await asyncio.gather(
            bot.start(DISCORD_BOT_TOKEN),   # Discord Bot
            start_fastapi(),                # FastAPI サーバ
        )

if __name__ == "__main__":
    asyncio.run(main())