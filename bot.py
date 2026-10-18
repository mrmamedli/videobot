"""Telegram-бот: присылаешь ссылку на видео, получаешь видео.

Поддерживаются X (Twitter), TikTok, YouTube, Xiaohongshu (RedNote).
Качество по умолчанию 720p; если файл не влезает в лимит Telegram (50 МБ),
бот пробует 480p, затем 360p.

Работает в личных сообщениях и в группах (см. ALLOWED_CHAT_IDS в .env).
"""

import asyncio
import logging
import os
import re
import shutil
import tempfile
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv
from yt_dlp.networking.impersonate import ImpersonateTarget

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

# Лимит Bot API на отправку файлов - 50 МБ, берём небольшой запас.
MAX_BYTES = 49 * 1024 * 1024
# Порядок попыток: сначала 720p, потом ниже.
HEIGHTS = (720, 480, 360)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov"}

SUPPORTED_DOMAINS = (
    "x.com",
    "twitter.com",
    "tiktok.com",
    "youtube.com",
    "youtu.be",
    "xiaohongshu.com",
    "xhslink.com",
)
URL_RE = re.compile(r"https?://[^\s<>\"']+")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("videobot")

# Скачиваем по одному видео за раз, чтобы не перегружать машину.
download_lock = asyncio.Semaphore(1)


class TooBigError(Exception):
    def __init__(self, size_bytes: int = 0):
        self.size_bytes = size_bytes
        super().__init__("video too big")


def is_supported(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


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


def download_video(url: str, workdir: Path) -> tuple[list[Path], dict]:
    """Блокирующая функция: качает видео, возвращает файлы и info."""
    url = resolve_short_url(url)
    biggest = 0
    for height in HEIGHTS:
        for p in workdir.iterdir():
            p.unlink()

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
        if COOKIES_FILE and Path(COOKIES_FILE).is_file():
            opts["cookiefile"] = COOKIES_FILE
        # TikTok пускает только "браузерные" запросы
        if (urlparse(url).hostname or "").lower().endswith("tiktok.com"):
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
    if "no video" in t or "there is no video" in t:
        return "Не нашёл в этой ссылке видео (возможно, там только фото или текст)."
    if "unsupported url" in t:
        return "Не получилось разобрать эту ссылку."
    short = re.sub(r"\x1b\[[0-9;]*m", "", text).strip()[:300]
    return f"Не получилось скачать. Ошибка: {short}"


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
        "Привет! Пришли ссылку на видео из X, TikTok, YouTube или Xiaohongshu, "
        "и я пришлю видео."
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
            files, info = await asyncio.to_thread(download_video, url, workdir)

        await status.edit_text("Отправляю…")
        single = len(files) == 1
        for path in files:
            await message.reply_video(
                FSInputFile(path),
                supports_streaming=True,
                width=info.get("width") if single else None,
                height=info.get("height") if single else None,
                duration=int(info["duration"]) if single and info.get("duration") else None,
            )
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

    session = AiohttpSession(timeout=300)  # отправка больших файлов может занять время
    bot = Bot(token=BOT_TOKEN, session=session)
    log.info("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
