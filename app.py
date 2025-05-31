# app.py  — Raspberry Pi (FastAPI + Discord Bot + ローカルエンコーダ)
import os, aiofiles, asyncio, subprocess, requests, uuid, pathlib, shutil, json, collections
import discord
from fastapi import FastAPI, UploadFile, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
ERROR_WEBHOOK_URL = os.getenv("ERROR_WEBHOOK_URL")
PC_WORKER_URL     = os.getenv("PC_WORKER_URL", "http://<PC-IP>:9000")  # 外部 PC
MAX_FILE_SIZE     = 9.9 * 1024 * 1024
MAX_RETRY_CRF     = [28, 31, 34, 37, 40]          # H.264 圧縮段階
QUEUE             = collections.deque()           # キュー表示

class BotRef:
    def __init__(self):
        self.bot: commands.Bot | None = None
        self.channel_id: int | None = None
        self.user_name:  str | None = None

bot_ref = BotRef()
app     = FastAPI()
tree: app_commands.CommandTree | None = None
asyncio_queue: asyncio.Queue = asyncio.Queue()

# ────────── ffprobe で解像度取得 ──────────
def get_resolution(path: str) -> tuple[int, int]:
    out = subprocess.check_output([
        "ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=width,height","-of","json",path
    ], text=True)
    info = json.loads(out)["streams"][0]
    return info["width"], info["height"]

# ────────── ffmpeg ラッパ ──────────
def _encode(src, dst, crf, codec="libx264", vf=None, log_path=None):
    cmd = [
        "nice","-n","10","ionice","-c2","-n7",
        "ffmpeg","-hide_banner","-y",
        "-analyzeduration","100M","-probesize","100M",
        "-i", src,
        # 不明ストリームを無視して映像1本＋音声1本だけ
        "-map","0:v:0","-map","0:a:0?","-ignore_unknown",
        "-movflags","faststart",
        "-vcodec", codec,
        "-crf", str(crf),
        "-preset","ultrafast",
        "-acodec","aac","-b:a","96k",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd.append(dst)
    with open(log_path or os.devnull, "wb") as logf:
        subprocess.run(cmd, check=True, stdout=logf, stderr=subprocess.STDOUT)

async def encode_async(src, dst, crf, codec="libx264", vf=None, log_path=None):
    await asyncio.to_thread(_encode, src, dst, crf, codec, vf, log_path)

# ────────── Pi ローカルエンコードタスク ──────────
async def process_video_local(job_id: str, workdir: pathlib.Path, ext: str):
    inputf  = workdir / f"input.{ext}"
    output  = workdir / "output.mp4"
    tmpfile = workdir / "tmp.mp4"
    logfile = workdir / "error.log"
    try:
        # ── 目標解像度リストを動的生成
        w, h = get_resolution(str(inputf))
        targets = []
        if max(w, h) <= 1080:
            targets.append((w, h))
        targets += [(1920,1080), (1280,720), (854,480)]

        # Pi4/5: HW H.264 があれば使う
        codec = "h264_v4l2m2m" if "h264_v4l2m2m" in subprocess.getoutput("ffmpeg -hide_banner -encoders") else "libx264"
        first_crf = 32 if ext == "mov" else 28
        success = False

        for tw, th in targets:
            vf = f"scale='min(iw,{tw})':'min(ih,{th})',fps=30"
            await encode_async(str(inputf), str(output), first_crf,
                               codec=codec, vf=vf, log_path=str(logfile))
            if output.stat().st_size <= MAX_FILE_SIZE:
                success = True
                break
            # CRF ステップ
            for crf in MAX_RETRY_CRF:
                await encode_async(str(output), str(tmpfile), crf,
                                   codec=codec, log_path=str(logfile))
                shutil.move(tmpfile, output)
                if output.stat().st_size <= MAX_FILE_SIZE:
                    success = True
                    break
            if success:
                break

        if not success:
            raise Exception("圧縮しても 9.9 MB 以下になりませんでした")

        # Discord 投稿 (3 回リトライ)
        channel = bot_ref.bot.get_channel(bot_ref.channel_id)  # type: ignore
        for i in range(3):
            try:
                await channel.send(
                    f"{bot_ref.user_name or '不明ユーザー'} さんの動画です。",
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
            data={"content": f"[動画投稿失敗] {e}"}, files=files)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

# ────────── 外部 PC に投げる ──────────
def try_dispatch_to_pc(workdir: pathlib.Path, ext: str, job_id: str) -> bool:
    try:
        with open(workdir / f"input.{ext}", "rb") as f:
            r = requests.post(f"{PC_WORKER_URL}/jobs",
                              files={"file": f},
                              data={"job_id": job_id, "ext": ext},
                              timeout=3)
        return r.status_code == 202
    except Exception:
        return False

# ────────── PC からの結果受信 ──────────
@app.put("/callback/{job_id}")
async def callback(job_id: str, file: UploadFile):
    out = pathlib.Path(tempfile.mkstemp(suffix=".mp4")[1])
    async with aiofiles.open(out, "wb") as f:
        await f.write(await file.read())
    channel = bot_ref.bot.get_channel(bot_ref.channel_id)  # type: ignore
    await channel.send(
        f"{bot_ref.user_name or '不明ユーザー'} さんの動画です。",
        file=discord.File(str(out))
    )
    os.remove(out)
    return PlainTextResponse("ok")

# ────────── Discord /vd ──────────
@app.on_event("startup")
async def startup_event():
    global tree
    while bot_ref.bot is None:
        await asyncio.sleep(0.1)

    tree = bot_ref.bot.tree
    if "vd" not in [c.name for c in tree.walk_commands()]:
        @tree.command(name="vd", description="アップロード URL を送信")
        async def vd(interaction: discord.Interaction):
            url = os.getenv("TUNNEL_URL", "URL未取得")
            await interaction.response.send_message(
                f"アップロードはこちら → {url}\n待機キュー: {len(QUEUE)} 件",
                ephemeral=True)
            bot_ref.channel_id = interaction.channel_id
            bot_ref.user_name  = interaction.user.name

    await bot_ref.bot.wait_until_ready()
    await tree.sync()
    asyncio.create_task(local_worker())

# Pi ローカルワーカー
async def local_worker():
    while True:
        job_id, workdir, ext = await asyncio_queue.get()
        await process_video_local(job_id, workdir, ext)
        asyncio_queue.task_done()

# ────────── アップロード受付 ──────────
@app.post("/")
async def upload(file: UploadFile, background_tasks: BackgroundTasks):
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(400, "動画のみ受け付けます")

    job_id   = uuid.uuid4().hex
    workdir  = pathlib.Path("static") / job_id
    workdir.mkdir(parents=True, exist_ok=True)
    ext      = file.filename.split(".")[-1].lower()
    savepath = workdir / f"input.{ext}"
    async with aiofiles.open(savepath, "wb") as f:
        await f.write(await file.read())

    dispatched = try_dispatch_to_pc(workdir, ext, job_id)
    if not dispatched:
        await asyncio_queue.put((job_id, workdir, ext))

    return HTMLResponse(
        f"受付完了！（待機 {len(QUEUE)} 件）<br>"
        "完了後 Discord に投稿されます。",
        status_code=202)

# ────────── 簡易フォーム ──────────
@app.get("/", response_class=HTMLResponse)
async def form():
    return """
    <html><head><title>動画アップロード</title></head>
    <body style="text-align:center;font-family:sans-serif;zoom:1.5">
      <h1>動画を選択</h1>
      <form action="/" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept="video/*" required><br>
        <input type="submit" value="アップロード">
      </form>
    </body></html>
    """
