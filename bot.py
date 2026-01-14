import os
import sys
import re
import uuid
import math
import asyncio
import logging
import subprocess
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pyrogram import Client, filters
from pyrogram.enums import ChatAction
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified
from yt_dlp import YoutubeDL

# ================= CONFIG =================
API_ID = 25800893
API_HASH = "6ccc441e2cbd1ef33ee523fd2349758b"
BOT_TOKEN = "8472411784:AAH--Dd1I45vwZcwLCi2iqav77vz8JfqpmI"
OWNER_ID = 6403142441
DUMP_CHAT_ID = -1002627919828

DOWNLOAD_DIR = "downloads"
THUMB_DIR = "thumbs"
MAX_TG_SIZE = 2 * 1024 * 1024 * 1024  # 2GB

HTTPS_PROXY = ""
SOCKS_PROXY = ""

# Site-specific cookies
COOKIES = {
    "youtube": os.path.join(DOWNLOAD_DIR, "youtube_cookies.txt"),
    "bilibili": os.path.join(DOWNLOAD_DIR, "bilibili_cookies.txt"),
    "dailymotion": os.path.join(DOWNLOAD_DIR, "dailymotion_cookies.txt"),
    "vimeo": os.path.join(DOWNLOAD_DIR, "vimeo_cookies.txt")
}

# ================= VALIDATE =================
required = {
    "API_ID": API_ID,
    "API_HASH": API_HASH,
    "BOT_TOKEN": BOT_TOKEN,
    "OWNER_ID": OWNER_ID,
    "DUMP_CHAT_ID": DUMP_CHAT_ID
}
missing = [k for k, v in required.items() if not v]
if missing:
    logging.error(f"❌ Missing required config: {', '.join(missing)}")
    sys.exit(1)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger('pyrogram').setLevel(logging.WARNING)

# ================= APP =================
app = Client(
    "yt_dlp_uploader",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=DOWNLOAD_DIR
)

# ================= SIGNALS =================
import signal
def signal_handler(sig, frame):
    logging.info("🛑 Shutdown signal received")
    app.stop()
    sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ================= HEALTH SERVER =================
def start_health_server():
    port = 8080
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ["/","/health"]:
                self.send_response(200)
                self.send_header("Content-type","text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_response(404)
                self.end_headers()
        def log_message(self, format, *args): return
    try:
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        logging.info(f"🌐 Health server listening on port {port}")
        server.serve_forever()
    except Exception as e:
        logging.error(f"Health server failed: {e}")
threading.Thread(target=start_health_server, daemon=True).start()

# ================= DATA =================
USERS = {}
ADMINS = {OWNER_ID}
ADDED_USERS = set()
ACTIVE_TASKS = {}
URL_RE = re.compile(r"https?://\S+")

# ================= HELPERS =================
def get_user(uid):
    USERS.setdefault(uid, {"caption": "", "caption_enabled": False})
    return USERS[uid]

def is_admin(uid):
    return uid in ADMINS

def is_valid_url(url):
    return bool(URL_RE.match(url))

def generate_thumbnail(video):
    thumb = os.path.join(THUMB_DIR, f"{uuid.uuid4()}.jpg")
    subprocess.run(["ffmpeg","-y","-i",video,"-ss","00:00:03","-vframes","1",thumb],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return thumb if os.path.exists(thumb) else None

def split_video(file):
    size = os.path.getsize(file)
    if size <= MAX_TG_SIZE:
        return [file]
    duration = float(subprocess.check_output([
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=nokey=1:noprint_wrappers=1",file
    ]).decode())
    parts=[]
    part_time = duration / math.ceil(size / MAX_TG_SIZE)
    start = 0
    i = 1
    while start < duration:
        out = f"{file}.part{i}.mp4"
        subprocess.run(
            ["ffmpeg","-y","-i",file,"-ss",str(start),"-t",str(part_time),"-c","copy",out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        parts.append(out)
        start += part_time
        i += 1
    return parts

def get_duration(file):
    try:
        out = subprocess.check_output([
            "ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=nokey=1:noprint_wrappers=1",file
        ]).decode().strip()
        return int(float(out))
    except:
        return 0

def format_size(num):
    for unit in ["B","KB","MB","GB","TB"]:
        if num<1024.0: return f"{num:.1f}{unit}"
        num/=1024.0
    return f"{num:.1f}PB"

def get_proxy_config():
    proxy_dict = {}
    if HTTPS_PROXY:
        proxy_dict["https"]=HTTPS_PROXY
        proxy_dict["http"]=HTTPS_PROXY
    if SOCKS_PROXY:
        proxy_dict["https"]=SOCKS_PROXY
        proxy_dict["http"]=SOCKS_PROXY
    return proxy_dict

def expand_url(url):
    try:
        proxies = get_proxy_config() or None
        resp = requests.head(url, allow_redirects=True, timeout=5, proxies=proxies)
        return resp.url
    except:
        return url

def get_cookie_file(url):
    if "youtube.com" in url or "youtu.be" in url:
        return COOKIES.get("youtube")
    elif "bilibili.com" in url:
        return COOKIES.get("bilibili")
    elif "dailymotion.com" in url:
        return COOKIES.get("dailymotion")
    elif "vimeo.com" in url:
        return COOKIES.get("vimeo")
    return None

# ================= DOWNLOAD =================
async def download_video(url,status=None):
    loop = asyncio.get_event_loop()
    url = await loop.run_in_executor(None, expand_url, url)
    uid = str(uuid.uuid4())
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{uid}_%(title)s.%(ext)s")
    if "-n " in url:
        parts = url.split("-n ",1)
        url=parts[0].strip()
        custom_name=parts[1].strip()
        outtmpl=os.path.join(DOWNLOAD_DIR,f"{uid}_{custom_name}.%(ext)s")

    ydl_opts={
        "format":"bestvideo+bestaudio/best",
        "merge_output_format":"mp4",
        "outtmpl":outtmpl,
        "quiet":True,
        "nocheckcertificate":True,
        "noplaylist": False,
        "concurrent_fragment_downloads": 5,
    }

    cookies_file = get_cookie_file(url)
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"]=cookies_file

    proxy = SOCKS_PROXY or HTTPS_PROXY
    if proxy: ydl_opts["proxy"]=proxy

    def progress_hook(d):
        try:
            st = d.get("status")
            if st=="downloading":
                downloaded=d.get("downloaded_bytes") or 0
                total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                text=f"⬇️ Downloading... {format_size(downloaded)}"
                if total:
                    text=f"⬇️ Downloading... {downloaded/total*100:.1f}% ({format_size(downloaded)}/{format_size(total)})"
                if status:
                    asyncio.run_coroutine_threadsafe(status.edit(text), loop)
            elif st=="finished":
                if status:
                    asyncio.run_coroutine_threadsafe(status.edit("⬇️ Download finished"), loop)
        except:
            pass

    ydl_opts["progress_hooks"]=[progress_hook]

    def run():
        with YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=True)

    info = await loop.run_in_executor(None, run)
    files=[os.path.join(DOWNLOAD_DIR,f) for f in os.listdir(DOWNLOAD_DIR) if f.startswith(uid)]
    return files, info

# ================= UPLOAD =================
async def upload_video(client,chat_id,file,caption,status=None):
    thumb=generate_thumbnail(file)
    parts=split_video(file)
    loop=asyncio.get_event_loop()
    def upload_progress(current,total):
        try:
            if total:
                pct=current/total*100
                text=f"📤 Uploading... {pct:.1f}% ({format_size(current)}/{format_size(total)})"
            else:
                text=f"📤 Uploading... {format_size(current)}"
            if status:
                asyncio.run_coroutine_threadsafe(status.edit(text),loop)
        except:
            pass
    try:
        for part in parts:
            await client.send_chat_action(chat_id,ChatAction.UPLOAD_VIDEO)
            await client.send_video(chat_id,part,caption=caption,thumb=thumb,
                                    supports_streaming=True,duration=get_duration(part),
                                    progress=upload_progress)
            await client.send_video(DUMP_CHAT_ID,part,
                                    caption=f"📤 From @{chat_id}\n\n{caption}" if caption else f"📤 From @{chat_id}",
                                    thumb=thumb,supports_streaming=True,duration=get_duration(part))
            os.remove(part)
    finally:
        if thumb and os.path.exists(thumb): os.remove(thumb)
        if os.path.exists(file): os.remove(file)

# ================= INLINE KB =================
def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 Help", callback_data="help")],
        [InlineKeyboardButton("📝 Caption", callback_data="caption"),
         InlineKeyboardButton("❌ Clear Caption", callback_data="clearcaption")],
        [InlineKeyboardButton("⛔ Cancel Task", callback_data="cancel")],
        [InlineKeyboardButton("📥 How to Download", callback_data="howtodownload")]
    ])

# ================= COMMANDS =================
@app.on_message(filters.command("start"))
async def start_cmd(_, m: Message):
    get_user(m.from_user.id)
    await m.reply("👋 Welcome! Send any video URL.\nUnlimited downloads", reply_markup=main_kb())

@app.on_message(filters.command("help"))
async def help_cmd(_, m: Message):
    await m.reply(
        "Commands:\n/start /help /caption /clearcaption /cancel /addadmin /adduser\n"
        "Custom Name: URL -n MyVideo", reply_markup=main_kb())

@app.on_message(filters.command("caption"))
async def set_caption(_, m: Message):
    user=get_user(m.from_user.id)
    text=m.text.replace("/caption","",1).strip()
    if text:
        user["caption"]=text
        user["caption_enabled"]=True
        await m.reply(f"✅ Caption set:\n{text}")
    else:
        await m.reply("❌ Usage: /caption Your text here")

@app.on_message(filters.command("clearcaption"))
async def clear_caption(_, m: Message):
    user=get_user(m.from_user.id)
    user["caption"]=""
    user["caption_enabled"]=False
    await m.reply("✅ Caption cleared")

@app.on_message(filters.command("cancel"))
async def cancel_task(_, m: Message):
    task=ACTIVE_TASKS.get(m.from_user.id)
    if task:
        task.cancel()
        await m.reply("❌ Task cancelled")
    else:
        await m.reply("No active task")

@app.on_message(filters.command("addadmin"))
async def addadmin_cmd(_, m: Message):
    if not is_admin(m.from_user.id): return await m.reply("❌ Admin only")
    if not m.reply_to_message: return await m.reply("Reply to user")
    uid=m.reply_to_message.from_user.id
    ADMINS.add(uid)
    ADDED_USERS.add(uid)
    await m.reply(f"✅ Admin added {uid}")

@app.on_message(filters.command("adduser"))
async def adduser_cmd(_, m: Message):
    if not is_admin(m.from_user.id): return await m.reply("❌ Admin only")
    if not m.reply_to_message: return await m.reply("Reply to user")
    uid=m.reply_to_message.from_user.id
    ADDED_USERS.add(uid)
    await m.reply(f"✅ Unlimited user {uid}")

# ================= CALLBACKS =================
@app.on_callback_query()
async def callbacks(_, q: CallbackQuery):
    uid=q.from_user.id
    user=get_user(uid)
    if q.data=="help":
        try:
            await q.message.edit_text("Send any video URL\nUnlimited downloads", reply_markup=main_kb())
        except MessageNotModified:
            pass
    elif q.data=="caption":
        await q.message.reply("Send: /caption your text")
    elif q.data=="clearcaption":
        user["caption"]=""; user["caption_enabled"]=False
        await q.message.reply("✅ Caption cleared")
    elif q.data=="cancel":
        task=ACTIVE_TASKS.get(uid)
        if task:
            task.cancel()
            await q.message.reply("❌ Task cancelled")
        else:
            await q.message.reply("No active task")
    elif q.data=="howtodownload":
        await q.message.reply("Send URL (single or playlist)\nCustom Name: URL -n MyVideo")
    await q.answer()

# ================= URL HANDLER =================
@app.on_message(filters.text & filters.private)
async def url_handler(client,m:Message):
    if m.text.startswith("/"): return
    if not is_valid_url(m.text):
        await m.reply("❌ Invalid URL")
        return
    user=get_user(m.from_user.id)
    caption=user["caption"] if user["caption_enabled"] else ""
    status=await m.reply("⬇️ Downloading...")

    async def job():  
        try:  
            files, info=await download_video(m.text,status=status)  
            try:  
                await status.edit("📤 Uploading...")  
            except:  
                pass  
            for f in files:  
                await upload_video(client,m.chat.id,f,caption or info.get("title","Video"),status=status)  
            try:  
                await status.delete()  
            except:  
                pass  
        except Exception as e:  
            err_msg=str(e)  
            if "Unsupported URL" in err_msg or "No video found" in err_msg:  
                err_text="❌ URL not supported or requires login/cookies. Supported: YouTube, Instagram, TikTok, Twitter, Bilibili, Dailymotion, Vimeo, etc."  
            else:  
                err_text=f"❌ Error:\n`{e}`"  
            try:  
                await status.edit(err_text)  
            except:  
                pass  

    ACTIVE_TASKS[m.from_user.id]=asyncio.create_task(job())

# ================= RUN =================
app.run()