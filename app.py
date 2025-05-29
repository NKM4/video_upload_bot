import os, aiofiles, asyncio, subprocess, requests, uuid, pathlib, shutil, collections
import discord
from fastapi import FastAPI, UploadFile, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
ERROR_WEBHOOK_URL = os.getenv("ERROR_WEBHOOK_URL")
MAX_FILE_SIZE      = 9.9 * 1024 * 1024            # 9.9 MB
MAX_RETRY_CRF      = [28, 31, 34, 37, 40]         # H.264 再圧縮
QUEUE              = collections.deque()          # 待機キュー表示用

class BotRef:
    def __init__(self):
        self.bot: commands.Bot | None = None
        self.last_channel_id: int | None = None
        self.last_user_name:  str | None = None

bot_ref = BotRef()
app     = FastAPI()
tree: app_commands.CommandTree | None = None

# ───────── ffmpeg ラッパ ─────────
def _encode(src: str, dst: str, crf: int, codec: str = "libx264", log_path: str | None = None):
    """nice/ionice で低優先度、stderr をログファイルへ"""
    with open(log_path or os.devnull, "wb") as logf:
        subprocess.run(
            ["nice","-n","10","ionice","-c2","-n7",
             "ffmpeg","-hide_banner","-y","-i",src,
             "-vcodec",codec,"-crf",str(crf),"-preset","fast",
             "-acodec","aac",dst],
            check=True, stdout=logf, stderr=subprocess.STDOUT
        )

async def encode_async(src: str, dst: str, crf: int,
                       codec: str = "libx264", log_path: str | None = None):
    await asyncio.to_thread(_encode, src, dst, crf, codec, log_path)

# ───────── 1タスク処理 ─────────
async def process_video(workdir: pathlib.Path, ext: str,
                        channel_id: int | None, user_name: str | None):
    output   = workdir / "output.mp4"
    inputf   = workdir / f"input.{ext}"
    logfile  = workdir / "error.log"
    try:
        # 初回 H.264
        first_crf = 32 if ext == "mov" else 28
        await encode_async(str(inputf), str(output), first_crf,
                           log_path=str(logfile))

        # 段階的再圧縮
        for crf in MAX_RETRY_CRF:
            if output.stat().st_size <= MAX_FILE_SIZE:
                break
            tmp = output.with_suffix(".tmp.mp4")
            await encode_async(str(output), str(tmp), crf,
                               log_path=str(logfile))
            shutil.move(tmp, output)

        # H.265 フォールバック
        if output.stat().st_size > MAX_FILE_SIZE:
            tmp = output.with_suffix(".x265.mp4")
            await encode_async(str(inputf), str(tmp), 28, "libx265",
                               log_path=str(logfile))
            if tmp.stat().st_size <= MAX_FILE_SIZE:
                shutil.move(tmp, output)
            else:
                raise Exception("H.265 でも 9.9 MB 以下に圧縮できませんでした")

        # Discord 送信（3回リトライ）
        channel = bot_ref.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            raise Exception("チャンネル取得に失敗")

        for i in range(3):
            try:
                await channel.send(
                    f"{user_name or '不明ユーザー'} さんの動画です。",
                    file=discord.File(str(output))
                )
                break
            except Exception:
                if i == 2:
                    raise
                await asyncio.sleep(5)

    except Exception as e:
        files = {"file": logfile.open("rb")} if logfile.exists() else None
        requests.post(ERROR_WEBHOOK_URL,
                      json={"content": f"[動画投稿失敗] {e}"},
                      files=files)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if QUEUE and QUEUE[0] == workdir:
            QUEUE.popleft()

# ───────── Bot /vd ─────────
@app.on_event("startup")
async def startup_event():
    global tree
    while bot_ref.bot is None:
        await asyncio.sleep(0.1)

    tree = bot_ref.bot.tree
    if "vd" not in [c.name for c in tree.walk_commands()]:
        @tree.command(name="vd", description="アップロード UI の URL を送信")
        async def send_url(interaction: discord.Interaction):
            url = os.getenv("TUNNEL_URL","URL未取得")
            await interaction.response.send_message(
                f"アップロードはこちら → {url}\n現在待機キュー: {len(QUEUE)} 件",
                ephemeral=True
            )
            bot_ref.last_channel_id = interaction.channel_id
            bot_ref.last_user_name  = interaction.user.name

    await bot_ref.bot.wait_until_ready()
    await tree.sync()

# ───────── 受付 ─────────
@app.post("/")
async def upload_video(request: Request,
                       file: UploadFile,
                       background_tasks: BackgroundTasks):
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="動画ファイルを選択してください")

    workdir = pathlib.Path("static") / uuid.uuid4().hex
    workdir.mkdir(parents=True, exist_ok=True)

    ext       = file.filename.split(".")[-1].lower()
    temp_path = workdir / f"input.{ext}"

    async with aiofiles.open(temp_path, "wb") as f:
        await f.write(await file.read())

    QUEUE.append(workdir)
    pos = len(QUEUE)

    background_tasks.add_task(
        process_video, workdir, ext,
        bot_ref.last_channel_id, bot_ref.last_user_name
    )

    return HTMLResponse(
        f"アップロードを受け付けました！（待機 {pos-1} 件）<br>"
        "数分以内に Discord に投稿されます。",
        status_code=202
    )

# ───────── フォーム ─────────
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
