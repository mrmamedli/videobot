"""Telegram-бот: присылаешь ссылку на видео или фото, получаешь их в ответ.

Поддерживаются X (Twitter), TikTok, YouTube, Xiaohongshu (RedNote).
Видео: качество по умолчанию 720p; если файл не влезает в лимит Telegram (50 МБ),
бот пробует 480p, затем 360p.
Фото и карусели (X, TikTok, Xiaohongshu): если в ссылке нет видео, бот достаёт
картинки и присылает их альбомами по 10 штук.

Работает в личных сообщениях и в группах (см. ALLOWED_CHAT_IDS в .env).
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InputMediaDocument, InputMediaPhoto, Message
from aiohttp import web
from dotenv import load_dotenv
from yt_dlp.networking import Request as YdlRequest
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import js_to_json

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_IDS = {
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
}
# Чаты (группы), где бот отвечает всем участникам. ID групп отрицательные.
ALLOWED_CHAT_IDS = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(" ", "").split(",") if x
}
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
# Для хостинга Render: он задаёт PORT и RENDER_EXTERNAL_URL сам.
# Если PORT есть, бот поднимает крошечный веб-сервер, а по адресу PING_URL
# сам себя «пингует», чтобы бесплатный сервис не засыпал.
PORT = int(os.getenv("PORT", "0") or 0)
PING_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_PING_URL") or "").strip()
PING_INTERVAL = 600  # секунд; Render усыпляет сервис после 15 минут тишины

# Лимит Bot API на отправку файлов - 50 МБ, берём небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
# Лимит Telegram на фото, отправляемое как фото (больше - отправим документом).
MAX_PHOTO_BYTES = 10 * 1024 * 1024
# Сколько картинок максимум присылать из одного поста.
MAX_IMAGES = 30
# Порядок попыток: сначала 720p, потом ниже.
HEIGHTS = (720, 480, 360)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}
# Эти форматы Telegram показывает как фото; остальные картинки уйдут документами.
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_EXTS = PHOTO_EXTS | {".gif", ".bmp", ".heic", ".heif", ".avif"}
CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/heic": ".heic",
}

SUPPORTED_DOMAINS = (
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "xiaohongshu.com",
    "xhslink.com",
)
# Для этих сервисов, если видео нет, пробуем достать фото.
IMAGE_DOMAINS = (
    "x.com",
    "twitter.com",
    "tiktok.com",
    "xiaohongshu.com",
    "xhslink.com",
)
URL_RE = re.compile(r"https?://[^\s<>\"']+")
XHS_NOTE_RE = re.compile(r"/(?:explore|discovery/item)/([\da-f]+)")
XHS_REFERER = {"Referer": "https://www.xiaohongshu.com/"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("videobot")

# Скачиваем по одному посту за раз, чтобы не перегружать машину.
download_lock = asyncio.Semaphore(1)


class TooBigError(Exception):
    def __init__(self, size_bytes: int = 0):
        self.size_bytes = size_bytes
        super().__init__("video too big")


def host_matches(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in domains)


def is_supported(url: str) -> bool:
    return host_matches(url, SUPPORTED_DOMAINS)


def extract_urls(message: Message) -> list[str]:
    urls = URL_RE.findall(message.text or message.caption or "")
    for ent in (message.entities or []) + (message.caption_entities or []):
        if ent.type == "text_link" and ent.url:
            urls.append(ent.url)
    # убираем знаки препинания в конце и дубли, сохраняя порядок
    cleaned = [u.rstrip(".,;:!?)»") for u in urls]
    return list(dict.fromkeys(cleaned))


def format_for(h: int) -> str:
    # Предпочитаем H.264 + AAC в mp4: такое видео Telegram проигрывает сразу.
    return (
        f"bv*[height<={h}][vcodec^=avc1]+ba[acodec^=mp4a]"
        f"/bv*[height<={h}][ext=mp4]+ba[ext=m4a]"
        f"/b[height<={h}][ext=mp4]"
        f"/bv*[height<={h}]+ba"
        f"/b[height<={h}]"
        f"/b"
    )


def approx_size(info: dict) -> int:
    formats = info.get("requested_formats") or [info]
    return sum((f.get("filesize") or f.get("filesize_approx") or 0) for f in formats)


def prepare_cookies() -> None:
    """yt-dlp пишет обратно в файл cookies, а секретный файл на хостинге доступен
    только для чтения, поэтому работаем с копией во временной папке."""
    global COOKIES_FILE
    if not has_cookies():
        if COOKIES_FILE:
            log.warning("COOKIES_FILE=%s не найден, cookies не используются", COOKIES_FILE)
            COOKIES_FILE = ""
        return
    copy = Path(tempfile.gettempdir()) / "cookies.txt"
    shutil.copyfile(COOKIES_FILE, copy)
    COOKIES_FILE = str(copy)
    log.info("cookies загружены")


def has_cookies() -> bool:
    return bool(COOKIES_FILE) and Path(COOKIES_FILE).is_file()


def clear_dir(workdir: Path) -> None:
    for p in workdir.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()


def natural_key(p: Path) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def resolve_short_url(url: str) -> str:
    """xhslink.com иногда редиректит на страницу входа; достаём из неё настоящую ссылку."""
    if not (urlparse(url).hostname or "").lower().endswith("xhslink.com"):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            final = resp.geturl()
    except Exception:
        return url
    parsed = urlparse(final)
    target = parse_qs(parsed.query).get("redirectPath")
    if "/login" in parsed.path and target:
        return target[0]
    return final


# ---------------------------------------------------------------- видео


def download_video(url: str, workdir: Path) -> tuple[list[Path], dict]:
    """Блокирующая функция: качает видео, возвращает файлы и info."""
    biggest = 0
    for height in HEIGHTS:
        clear_dir(workdir)

        opts = {
            "format": format_for(height),
            "merge_output_format": "mp4",
            "outtmpl": str(workdir / "video_%(autonumber)s.%(ext)s"),
            "noplaylist": False,
            "playlistend": 4,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": MAX_BYTES,
            "socket_timeout": 30,
            "retries": 3,
        }
        if has_cookies():
            opts["cookiefile"] = COOKIES_FILE
        # TikTok пускает только "браузерные" запросы
        if host_matches(url, ("tiktok.com",)):
            opts["impersonate"] = ImpersonateTarget("chrome")

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        biggest = max(biggest, approx_size(info or {}))
        files = sorted(
            p for p in workdir.iterdir() if p.suffix.lower() in VIDEO_EXTS
        )
        if files and all(p.stat().st_size <= MAX_BYTES for p in files):
            return files, info or {}
        for p in files:
            biggest = max(biggest, p.stat().st_size)
        log.info("%s: %sp не влезло в лимит, пробую ниже", url, height)

    raise TooBigError(biggest)


# ---------------------------------------------------------------- фото


def parse_xhs_images(webpage: str, note_id: str) -> list[str]:
    """Достаёт ссылки на картинки заметки из window.__INITIAL_STATE__ страницы Xiaohongshu."""
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", webpage)
    if not m:
        return []
    raw = webpage[m.end():]
    end = raw.find("</script>")
    if end != -1:
        raw = raw[:end]
    try:
        state, _ = json.JSONDecoder().raw_decode(js_to_json(raw.strip()))
    except ValueError:
        return []

    details = ((state.get("note") or {}).get("noteDetailMap")) or {}
    entry = details.get(note_id)
    if not (entry or {}).get("note"):
        entry = next((v for v in details.values() if (v or {}).get("note")), None)
    note = (entry or {}).get("note") or {}

    urls: list[str] = []
    for img in note.get("imageList") or []:
        u = img.get("urlDefault")
        if not u:
            for info in img.get("infoList") or []:
                if info.get("imageScene") == "WB_DFT" and info.get("url"):
                    u = info["url"]
                    break
        u = u or img.get("urlPre")
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("http://"):
            u = "https://" + u[len("http://"):]
        urls.append(u)
    return urls


def download_xhs_images(url: str, workdir: Path) -> list[Path]:
    """Xiaohongshu: gallery-dl этот сайт не знает, поэтому разбираем страницу сами."""
    m = XHS_NOTE_RE.search(url)
    if not m:
        return []
    opts = {"quiet": True, "no_warnings": True, "socket_timeout": 30}
    if has_cookies():
        opts["cookiefile"] = COOKIES_FILE

    files: list[Path] = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        page = ydl.urlopen(YdlRequest(url, headers=XHS_REFERER)).read()
        img_urls = parse_xhs_images(page.decode("utf-8", "replace"), m.group(1))
        for i, img_url in enumerate(img_urls[:MAX_IMAGES], 1):
            resp = ydl.urlopen(YdlRequest(img_url, headers=XHS_REFERER))
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            path = workdir / f"{i:03d}{CONTENT_TYPE_EXT.get(ctype, '.jpg')}"
            path.write_bytes(data)
            files.append(path)
    return files


def download_images_gallerydl(url: str, workdir: Path) -> list[Path]:
    """X и TikTok: картинки качает gallery-dl (запускаем его как отдельную программу)."""
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "-D", str(workdir),
        "--http-timeout", "30",
    ]
    if has_cookies():
        cmd += ["-C", COOKIES_FILE]
    cmd.append(url)

    proc = subprocess.run(
        cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=180
    )
    files = sorted(
        (p for p in workdir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
        key=natural_key,
    )
    if not files:
        log.warning(
            "gallery-dl не нашёл картинок для %s (код %s): %s",
            url, proc.returncode, (proc.stderr or proc.stdout).strip()[-500:],
        )
    return files


def download_images(url: str, workdir: Path) -> list[Path]:
    if host_matches(url, ("xiaohongshu.com", "xhslink.com")):
        return download_xhs_images(url, workdir)
    return download_images_gallerydl(url, workdir)


def fetch_media(url: str, workdir: Path) -> tuple[str, list[Path], dict]:
    """Возвращает ('video', файлы, info) или ('images', файлы, {})."""
    url = resolve_short_url(url)
    try:
        files, info = download_video(url, workdir)
        return "video", files, info
    except yt_dlp.utils.DownloadError as video_error:
        if not host_matches(url, IMAGE_DOMAINS):
            raise
        log.info("Видео не нашлось (%s), ищу фото: %s", str(video_error)[:120], url)
        clear_dir(workdir)
        try:
            images = download_images(url, workdir)
        except Exception:
            log.exception("Не получилось скачать фото для %s", url)
            images = []
        if images:
            return "images", images, {}
        raise video_error


def explain_error(text: str) -> str:
    t = text.lower()
    if "not a bot" in t or "sign in to confirm" in t:
        return (
            "YouTube просит подтвердить, что я не бот. Нужен файл cookies.txt "
            "(см. README, раздел про cookies)."
        )
    if "private" in t or "log in" in t or "login" in t or "cookies" in t:
        return "Видео приватное или требует входа в аккаунт. Нужен cookies.txt (см. README)."
    if "removed" in t or "deleted" in t or "unavailable" in t or "not available" in t:
        return "Видео недоступно: удалено, скрыто или закрыто в моём регионе."
    if "no video" in t or "there is no video" in t or "no video formats" in t:
        return (
            "Не нашёл в этой ссылке ни видео, ни фото. Возможно, пост удалён "
            "или закрыт для просмотра без входа в аккаунт."
        )
    if "unsupported url" in t:
        return "Не получилось разобрать эту ссылку."
    short = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()[:300]
    return f"Не получилось скачать. Ошибка: {short}"


# ---------------------------------------------------------------- отправка


async def with_retry(make_call):
    """Повторяет отправку, если Telegram просит подождать (лимит частоты сообщений)."""
    for attempt in range(3):
        try:
            return await make_call()
        except TelegramRetryAfter as e:
            if attempt == 2:
                raise
            await asyncio.sleep(e.retry_after + 1)


async def send_photo_chunk(message: Message, chunk: list[Path]) -> None:
    if len(chunk) == 1:
        await with_retry(lambda: message.reply_photo(FSInputFile(chunk[0])))
    else:
        media = [InputMediaPhoto(media=FSInputFile(p)) for p in chunk]
        await with_retry(lambda: message.reply_media_group(media))


async def send_document_chunk(message: Message, chunk: list[Path]) -> None:
    if len(chunk) == 1:
        await with_retry(lambda: message.reply_document(FSInputFile(chunk[0])))
    else:
        media = [InputMediaDocument(media=FSInputFile(p)) for p in chunk]
        await with_retry(lambda: message.reply_media_group(media))


async def send_images(message: Message, files: list[Path]) -> None:
    """Фото уходят альбомами по 10; крупные или нестандартные файлы - документами."""
    photos = [
        p for p in files
        if p.suffix.lower() in PHOTO_EXTS and p.stat().st_size <= MAX_PHOTO_BYTES
    ]
    documents = [p for p in files if p not in photos]

    for i in range(0, len(photos), 10):
        chunk = photos[i:i + 10]
        try:
            await send_photo_chunk(message, chunk)
        except TelegramBadRequest as e:
            # например, слишком большие размеры картинки: шлём без сжатия
            log.warning("Telegram не принял фото (%s), отправляю документами", e)
            documents.extend(chunk)
        await asyncio.sleep(0.5)

    for i in range(0, len(documents), 10):
        await send_document_chunk(message, documents[i:i + 10])
        await asyncio.sleep(0.5)


# ---------------------------------------------------------------- бот

def allowed(message: Message) -> bool:
    if message.chat.id in ALLOWED_CHAT_IDS:
        return True
    return bool(message.from_user) and message.from_user.id in ALLOWED_IDS


dp = Dispatcher()


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"Твой Telegram ID: {message.from_user.id}\nID этого чата: {message.chat.id}"
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not allowed(message):
        await message.answer(
            "Это приватный бот. Если ты его владелец, команда /id покажет твой ID, "
            "его нужно вписать в ALLOWED_USER_IDS."
        )
        return
    await message.answer(
        "Привет! Пришли ссылку на видео или фото из X, TikTok, YouTube или Xiaohongshu, "
        "и я пришлю их."
    )


@dp.message()
async def handle_message(message: Message):
    if not allowed(message):
        return

    is_private = message.chat.type == "private"
    urls = extract_urls(message)
    if not urls:
        if is_private:
            await message.answer("Пришли ссылку на видео.")
        return

    for url in urls:
        if not is_supported(url):
            if is_private:
                await message.answer(
                    f"Такой сервис я не знаю: {url}\n"
                    "Умею: X, TikTok, YouTube, Xiaohongshu."
                )
            continue
        await process_url(message, url)


async def process_url(message: Message, url: str):
    status = await message.answer("Скачиваю…")
    workdir = Path(tempfile.mkdtemp(prefix="tgvid_"))
    try:
        async with download_lock:
            kind, files, info = await asyncio.to_thread(fetch_media, url, workdir)

        await status.edit_text("Отправляю…")
        if kind == "video":
            single = len(files) == 1
            for path in files:
                await message.reply_video(
                    FSInputFile(path),
                    supports_streaming=True,
                    width=info.get("width") if single else None,
                    height=info.get("height") if single else None,
                    duration=int(info["duration"]) if single and info.get("duration") else None,
                )
        else:
            await send_images(message, files[:MAX_IMAGES])
        await status.delete()

    except TooBigError as e:
        size = f" (около {e.size_bytes // (1024 * 1024)} МБ)" if e.size_bytes else ""
        await status.edit_text(
            f"Видео слишком большое для Telegram{size}: даже в 360p оно не влезает в 50 МБ."
        )
    except yt_dlp.utils.DownloadError as e:
        log.warning("DownloadError для %s: %s", url, e)
        await status.edit_text(explain_error(str(e)))
    except Exception:
        log.exception("Неожиданная ошибка для %s", url)
        await status.edit_text("Что-то пошло не так. Подробности в логах бота.")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_web_server() -> web.AppRunner:
    """Render требует, чтобы сервис слушал порт. Отвечаем «ok» на / и /health."""
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Веб-сервер слушает порт %s", PORT)
    return runner


async def keep_alive_loop(url: str) -> None:
    """Раз в 10 минут заходим на собственный адрес, чтобы Render не усыплял бота."""
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                async with http.get(url) as resp:
                    await resp.read()
            except Exception as e:
                log.warning("Самопинг не удался: %s", e)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Скопируй .env.example в .env и заполни его.")
    if not ALLOWED_IDS and not ALLOWED_CHAT_IDS:
        log.warning(
            "ALLOWED_USER_IDS и ALLOWED_CHAT_IDS пусты: бот отвечает только на /id. "
            "Узнай свой ID, впиши его в .env и перезапусти бота."
        )
    if shutil.which("ffmpeg") is None:
        log.warning("ffmpeg не найден: склейка видео и звука работать не будет.")

    prepare_cookies()

    runner = None
    ping_task = None
    if PORT:
        runner = await start_web_server()
        if PING_URL:
            ping_task = asyncio.create_task(keep_alive_loop(PING_URL))

    session = AiohttpSession(timeout=300)  # отправка больших файлов может занять время
    bot = Bot(token=BOT_TOKEN, session=session)
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot)
    finally:
        if ping_task:
            ping_task.cancel()
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
