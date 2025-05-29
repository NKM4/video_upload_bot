import os, aiofiles, asyncio, subprocess, requests, uuid, pathlib, shutil, collections, mimetypes
import discord
from fastapi import FastAPI, UploadFile, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
ERROR_WEBHOOK_URL = os.getenv("ERROR_WEBHOOK_URL")
MAX_FILE_SIZE      = 9.9 * 1024 * 1024            # 9.9 MB
MAX_RETRY_CRF      = [28, 31, 34, 37, 40]         # H.264 再圧縮用
QUEUE              = collections.deque()          # ② 待機キュー

class BotRef:
    def __init__(self):
        self.bot: commands.Bot | None = None
        self.last_channel_id: int | None = None
        self.last_user_name:  str | None = None

bot_ref = BotRef()
app     = FastAPI()
tree: app_commands.CommandTree | None = None      # 遅延取得

# ────────────── ffmpeg ヘルパ ──────────────
def _encode(src: str, dst: str, crf: int, codec: str = "libx264"):
    """nice + ionice で CPU/I/O 優先度を下げて実行"""
    subprocess.run(
        ["nice", "-n", "10", "ionice", "-c2", "-n7",
         "ffmpeg", "-y", "-i", src,
         "-vcodec", codec, "-crf", str(crf), "-preset", "fast",
         "-acodec", "aac", dst],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

async def encode_async(src: str, dst: str, crf: int, codec: str = "libx264"):
    await asyncio.to_thread(_encode, src, dst, crf, codec)

# ────────────── 1 タスク処理 ──────────────
async def process_video(workdir: pathlib.Path, ext: str,
                        channel_id: int | None, user_name: str | None):
    output = workdir / "output.mp4"
    inputf = workdir / f"input.{ext}"
    try:
        # 初回 H.264 エンコード
        first_crf = 32 if ext == "mov" else 28
        await encode_async(str(inputf), str(output), crf=first_crf)

        # サイズ超過なら段階的に H.264 再圧縮
        for crf in MAX_RETRY_CRF:
            if output.stat().st_size <= MAX_FILE_SIZE:
                break
            tmp = output.with_suffix(".tmp.mp4")
            await encode_async(str(output), str(tmp), crf=crf)
            shutil.move(tmp, output)

        # まだ超過 → H.265 フォールバック
        if output.stat().st_size > MAX_FILE_SIZE:
            tmp = output.with_suffix(".x265.mp4")
            await encode_async(str(inputf), str(tmp), crf=28, codec="libx265")
            if tmp.stat().st_size <= MAX_FILE_SIZE:
                shutil.move(tmp, output)
            else:
                raise Exception("H.265 でも 9.9 MB 以下に圧縮できませんでした")

        # Discord 投稿（⑥ 3 回までリトライ）
        channel = bot_ref.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            raise Exception("チャンネル取得に失敗しました")

        for attempt in range(3):
            try:
                await channel.send(
                    f"{user_name or '不明ユーザー'} さんの動画です。",
                    file=discord.File(str(output))
                )
                break
            except Exception as e:
                if attempt == 2:
                    raise
                await asyncio.sleep(5)

    except Exception as e:
        requests.post(ERROR_WEBHOOK_URL,
                      json={"content": f"[動画投稿失敗] {e}"})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if QUEUE and QUEUE[0] == workdir:
            QUEUE.popleft()

# ────────────── Bot /vd コマンド ──────────────
@app.on_event("startup")
async def startup_event():
    global tree
    while bot_ref.bot is None:
        await asyncio.sleep(0.1)

    tree = bot_ref.bot.tree
    if "vd" not in [cmd.name for cmd in tree.walk_commands()]:
        @tree.command(name="vd", description="アップロード UI の URL を送信")
        async def send_url(interaction: discord.Interaction):
            url = os.getenv("TUNNEL_URL", "URL未取得")
            await interaction.response.send_message(
                f"アップロードはこちら → {url}\n"
                f"現在の待機キュー: {len(QUEUE)} 件",
                ephemeral=True
            )
            bot_ref.last_channel_id = interaction.channel_id
            bot_ref.last_user_name  = interaction.user.name

    await bot_ref.bot.wait_until_ready()
    await tree.sync()

# ────────────── アップロード受付 ──────────────
@app.post("/")
async def upload_video(
    request: Request,
    file: UploadFile,
    background_tasks: BackgroundTasks
):
    # ④ ファイル形式チェック
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="動画ファイルを選択してください")

    workdir = pathlib.Path("static") / uuid.uuid4().hex
    workdir.mkdir(parents=True, exist_ok=True)

    ext       = file.filename.split(".")[-1].lower()
    temp_path = workdir / f"input.{ext}"

    async with aiofiles.open(temp_path, "wb") as f:
        await f.write(await file.read())

    QUEUE.append(workdir)  # ② キューに登録
    position = len(QUEUE)

    background_tasks.add_task(
        process_video,
        workdir, ext,
        bot_ref.last_channel_id, bot_ref.last_user_name
    )

    return HTMLResponse(
        f"アップロードを受け付けました！（現在待機 {position-1} 件）<br>"
        "数分以内に Discord に投稿されます。",
        status_code=202
    )

# ────────────── シンプル UI ──────────────
@app.get("/", response_class=HTMLResponse)
async def get_form():
    return """
    <html>
      <head>
        <title>動画アップロード</title>
        <style>
          body { font-family: sans-serif; text-align: center; margin-top: 40px; zoom: 1.5; }
          input { margin: 10px; font-size: 18px; }
        </style>
      </head>
      <body>
        <h1>動画を選択してください</h1>
        <form action="/" enctype="multipart/form-data" method="post">
          <input name="file" type="file" accept="video/*" required>
          <input type="submit" value="アップロード">
        </form>
      </body>
    </html>
    """