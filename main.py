import logging
import re
import os
import sys
import time
import asyncio
from aiohttp import web

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional, Tuple, Dict
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from functools import wraps

import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from telegram.error import Forbidden, BadRequest, TelegramError, RetryAfter
from telegram.helpers import escape_markdown
from apscheduler.schedulers.asyncio import AsyncIOScheduler

UZ_TZ = ZoneInfo('Asia/Tashkent')


def _env_bool(name: str, default: bool = False) -> bool:
    """ENV dan bool o'qish: 1/true/yes/on/ha -> True."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on', 'ha')


def _env_ids(name: str) -> set:
    """ENV dan vergul/bo'shliq bilan ajratilgan ID ro'yxatini o'qish."""
    out = set()
    for part in re.split(r'[,\s;]+', os.getenv(name, "") or ""):
        part = part.strip()
        if part.lstrip('-').isdigit():
            out.add(int(part))
    return out


@dataclass
class Config:
    """Centralized bot configuration"""
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0") or "0")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")
    PORT: int = int(os.getenv("PORT", "8080"))

    BASE_DIR: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))
    DB_FILE: str = field(init=False)
    BACKUP_DIR: str = field(init=False)
    ADMIN_IDS: frozenset = field(init=False)

    # Qo'shimcha adminlar: ADMIN_IDS=111,222 (ADMIN_ID asosiy — bildirishnomalar unga ketadi)
    # Bot o'chganda kelgan xabarlar tashlab yuborilsinmi (default: YO'Q, hammasi ko'rib chiqiladi)
    DROP_PENDING_UPDATES: bool = field(default_factory=lambda: _env_bool("DROP_PENDING_UPDATES", False))
    # Oddiy xabarlar matni bazaga yozilsinmi (default: YO'Q — maxfiylik.
    # Spam/flood xabarlari dalil sifatida har holda saqlanadi)
    LOG_MESSAGE_TEXT: bool = field(default_factory=lambda: _env_bool("LOG_MESSAGE_TEXT", False))
    # Har qanday @username reklama deb hisoblansinmi (default: YO'Q — oddiy tag jazolanmaydi)
    BLOCK_MENTIONS: bool = field(default_factory=lambda: _env_bool("BLOCK_MENTIONS", False))
    # Nomidan erkin yozishi mumkin bo'lgan kanallar: ALLOWED_CHANNELS=@mening_kanalim,-1001234567890
    ALLOWED_CHANNEL_IDS: frozenset = field(init=False)
    ALLOWED_CHANNEL_USERNAMES: frozenset = field(init=False)

    MAX_WARNS: int = 3
    MUTE_DURATION_MINUTES: int = 10
    FLOOD_LIMIT: int = 3
    FLOOD_WINDOW_SECONDS: int = 5
    WARNING_DELETE_DELAY: int = 10
    BROADCAST_DELAY: float = 0.3
    FLOOD_CLEANUP_MINUTES: int = 5

    DB_TIMEOUT: int = 10

    PM_FLOOD_LIMIT: int = 5
    PM_FLOOD_WINDOW_SECONDS: int = 10
    ADMIN_REFRESH_DELAY: float = 0.4
    BACKUP_KEEP: int = 7

    LINK_PATTERNS: Tuple[re.Pattern, ...] = field(default_factory=tuple)

    def __post_init__(self):

        # DB_PATH env orqali persistent volume (Cloud Run/Docker) ga yo'naltirish mumkin.
        # Cloud Run'da lokal disk vaqtinchalik — baza yo'qolmasligi uchun volume ulang.
        self.DB_FILE = os.getenv("DB_PATH") or os.path.join(self.BASE_DIR, "bot_data.db")
        self.BACKUP_DIR = os.getenv("BACKUP_DIR") or os.path.join(self.BASE_DIR, "backups")

        # BOT_USERNAME oldidagi "@" ni olib tashlaymiz — aks holda
        # "https://t.me/@user?startgroup=true" havolasi buziladi
        self.BOT_USERNAME = (self.BOT_USERNAME or "").strip().lstrip('@')

        ids = _env_ids("ADMIN_IDS")
        if self.ADMIN_ID:
            ids.add(self.ADMIN_ID)
        elif ids:
            self.ADMIN_ID = sorted(ids)[0]
        self.ADMIN_IDS = frozenset(ids)

        chan_ids, chan_names = set(), set()
        for part in re.split(r'[,\s;]+', os.getenv("ALLOWED_CHANNELS", "") or ""):
            part = part.strip()
            if not part:
                continue
            if part.lstrip('-').isdigit():
                chan_ids.add(int(part))
            else:
                chan_names.add(part.lstrip('@').lower())
        self.ALLOWED_CHANNEL_IDS = frozenset(chan_ids)
        self.ALLOWED_CHANNEL_USERNAMES = frozenset(chan_names)

        patterns = [
            r'http[s]?://(?:[a-zA-Z0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
            r'www\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,}',
            r't\.me/[a-zA-Z0-9_+]+',
            r'(?:telegram\.me|telegram\.dog)/[a-zA-Z0-9_+]+',
            # DIQQAT: @mention bu yerda YO'Q. Oddiy "@ali" tagi reklama emas —
            # u contains_link() ichida reklama konteksti bilan birga tekshiriladi.
            # domen: oldida harf/raqam yo'q (versiya "2.5" yoki "file.com" so'z ichida tutilmaydi),
            # keyin ham qo'shimcha harf bo'lmasligi
            r'(?<![a-zA-Z0-9.@-])[a-zA-Z][a-zA-Z0-9-]{1,}\.(com|net|org|uz|ru|me|io|co|info|online|site|kg|tj)\b',
            r'bit\.ly/[a-zA-Z0-9]+',
            r'tinyurl\.com/[a-zA-Z0-9]+',
        ]
        self.LINK_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in patterns)

    def is_allowed_channel(self, sender_chat) -> bool:
        """Shu kanal nomidan yozishga ruxsat berilganmi (ALLOWED_CHANNELS)."""
        if sender_chat is None:
            return False
        if sender_chat.id in self.ALLOWED_CHANNEL_IDS:
            return True
        uname = (getattr(sender_chat, 'username', None) or '').lower()
        return bool(uname) and uname in self.ALLOWED_CHANNEL_USERNAMES

    def validate(self) -> bool:
        if not self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is required")
        if not self.ADMIN_ID:
            raise ValueError("ADMIN_ID is required (yoki ADMIN_IDS)")
        if not self.BOT_USERNAME:
            logger.warning("BOT_USERNAME bo'sh — \"Guruhga qo'shish\" tugmasi ishlamaydi")
        if self.WEBHOOK_URL and not self.WEBHOOK_SECRET:
            logger.warning("WEBHOOK_SECRET bo'sh — webhook URL'ni bilgan har kim soxta xabar yuborishi mumkin")
        return True


config = Config()


def setup_logging() -> logging.Logger:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'

    logging.basicConfig(
        format=log_format,
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)

    return logging.getLogger(__name__)


logger = setup_logging()


def is_bot_admin(user_id: Optional[int]) -> bool:
    """Bot egasi yoki ADMIN_IDS ro'yxatidagi qo'shimcha adminmi."""
    return bool(user_id) and user_id in config.ADMIN_IDS


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if not is_bot_admin(user_id):
            if update.callback_query:
                await update.callback_query.answer("❌ Sizda ruxsat yo'q!", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def error_handler(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except TelegramError as e:
            logger.error(f"Telegram error in {func.__name__}: {e}")
            raise
        except Exception as e:
            logger.exception(f"Unexpected error in {func.__name__}: {e}")
            raise
    return wrapper


# Telegram xizmat akkauntlari (rasmiy, o'zgarmas ID lar)
GROUP_ANONYMOUS_BOT_ID = 1087968824   # anonim admin nomidan yuborilgan xabarlar
CHANNEL_BOT_ID = 136817688            # kanal nomidan yuborilgan xabarlar


class FloodControl:
    """Professional flood control without global locks"""

    def __init__(self, limit: int = 3, window: int = 5):
        self._data = {}
        self._limit = limit
        self._window = window

    async def check(self, user_id: int, is_bot: bool = False, is_admin: bool = False,
                    chat_id: Optional[int] = None) -> bool:
        if is_admin or is_bot:
            return False

        # Hisoblagich HAR GURUH uchun alohida: odam 3 xil guruhda bittadan yozsa
        # bu flood emas (ilgari shunday hisoblanib, xabari o'chirilardi).
        key = (chat_id, user_id) if chat_id is not None else user_id

        now = asyncio.get_event_loop().time()

        if key not in self._data:
            self._data[key] = [now]
            return False

        self._data[key] = [t for t in self._data[key] if now - t < self._window]
        self._data[key].append(now)

        is_flooding = len(self._data[key]) > self._limit

        if is_flooding:
            logger.warning(f"🚫 FLOOD: User {user_id} sent {len(self._data[key])} messages in {self._window}s (chat {chat_id})")

        return is_flooding

    async def cleanup(self) -> int:
        now = asyncio.get_event_loop().time()
        stale_keys = [
            key for key, times in self._data.items()
            if all(now - t >= self._window for t in times)
        ]
        for key in stale_keys:
            del self._data[key]

        if stale_keys:
            logger.debug(f"Cleaned {len(stale_keys)} flood control entries")
        return len(stale_keys)

    def get_stats(self):
        return {
            'tracked_users': len(self._data),
            'total_messages': sum(len(times) for times in self._data.values())
        }

flood_control = FloodControl(config.FLOOD_LIMIT, config.FLOOD_WINDOW_SECONDS)
# Shaxsiy chat uchun alohida, yumshoqroq limit — adminning DM'i flood qilinmasligi uchun
pm_flood_control = FloodControl(config.PM_FLOOD_LIMIT, config.PM_FLOOD_WINDOW_SECONDS)


class AdminManager:
    """Enterprise-level admin management without global bottlenecks"""

    def __init__(self):
        self._admins = {}
        self._last_refresh = {}
        self._refreshing = {}  # To prevent duplicate API calls for the same chat
        self._refresh_interval = 600
        self._bot_id = None
        self._lock = asyncio.Lock()
        logger.info("✅ AdminManager initialized")

    async def refresh_admins(self, bot, chat_id: int) -> bool:
        if self._refreshing.get(chat_id):
            return False
            
        self._refreshing[chat_id] = True
        try:
            # Network call without global lock!
            admins = await bot.get_chat_administrators(chat_id)
            admin_ids = {admin.user.id for admin in admins}
            admin_ids |= set(config.ADMIN_IDS)

            self._admins[chat_id] = admin_ids
            self._last_refresh[chat_id] = asyncio.get_event_loop().time()

            logger.info(f"🔄 Admin list refreshed for chat {chat_id}: {len(admin_ids)} admins")
            return True
        except Exception as e:
            logger.error(f"Failed to refresh admins for chat {chat_id}: {e}")
            return False
        finally:
            self._refreshing[chat_id] = False

    def is_fresh(self, chat_id: int) -> bool:
        """Kesh hali eskirmaganmi (keraksiz API chaqiruvlarini oldini oladi)."""
        last = self._last_refresh.get(chat_id, 0)
        return bool(self._admins.get(chat_id)) and (asyncio.get_event_loop().time() - last) < self._refresh_interval

    def prune(self, active_chat_ids) -> None:
        """Faol bo'lmagan guruhlar keshini tozalaydi (memory leak oldini olish)."""
        active = set(active_chat_ids)
        for cache in (self._admins, self._last_refresh, self._refreshing):
            for cid in list(cache.keys()):
                if cid not in active:
                    del cache[cid]

    def get_stats(self):
        return {
            'cached_chats': len(self._admins),
            'total_admins': sum(len(admins) for admins in self._admins.values())
        }

admin_manager = AdminManager()


async def is_definitely_admin(context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int,
                              user_id: int,
                              user) -> Tuple[bool, str]:
    """Universal admin verification - PRODUCTION READY"""

    # LAYER 0: Anonymous Admin Messages (Telegram official).
    # Username ba'zan bo'sh kelishi mumkin — shuning uchun ID bo'yicha ham tekshiramiz.
    if user.is_bot and (user.username == "GroupAnonymousBot" or user.id == GROUP_ANONYMOUS_BOT_ID):
        logger.info(f"✅ LAYER 0: Anonymous admin message detected")
        return (True, "anonymous_admin")

    # LAYER 1: Bot owner
    if is_bot_admin(user_id):
        logger.info(f"🔥 LAYER 1: Bot owner {user_id}")
        return (True, "bot_owner")

    # LAYER 2: Cache
    try:
        if chat_id not in admin_manager._admins or not admin_manager._admins[chat_id]:
            logger.warning(f"⚠️ Admin cache empty for {chat_id}, refreshing...")
            await admin_manager.refresh_admins(context.bot, chat_id)

        if user_id in admin_manager._admins.get(chat_id, set()):
            logger.info(f"✅ LAYER 2: User {user_id} found in cache")
            return (True, "cache")

    except Exception as cache_error:
        logger.error(f"❌ LAYER 2 FAILED: {cache_error}")

    # LAYER 3: API
    try:
        logger.info(f"🔄 LAYER 3: API fallback for user {user_id}")
        member = await context.bot.get_chat_member(chat_id, user_id)

        if member.status in ['creator', 'administrator']:
            logger.info(f"✅ LAYER 3: User {user_id} verified as {member.status}")

            try:
                async with admin_manager._lock:
                    if chat_id not in admin_manager._admins:
                        admin_manager._admins[chat_id] = set()
                    admin_manager._admins[chat_id].add(user_id)
                    admin_manager._last_refresh[chat_id] = asyncio.get_event_loop().time()
            except Exception as cache_write_error:
                logger.warning(f"Admin keshiga yozib bo'lmadi: {cache_write_error}")

            return (True, "api_fallback")
        else:
            logger.info(f"👤 LAYER 3: User {user_id} is NOT admin")
            return (False, "verified_non_admin")

    except Exception as api_error:
        logger.error(f"❌ LAYER 3 FAILED: {api_error}")
        return (False, "verification_failed")


class Database:
    """Async database manager (Simple SQLite)"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_lock = asyncio.Lock()
        self._conn = None
        self._conn_lock = asyncio.Lock()

    async def _connect(self):
        conn = await aiosqlite.connect(self.db_path, timeout=config.DB_TIMEOUT)
        conn.row_factory = aiosqlite.Row
        await conn.execute('PRAGMA journal_mode=WAL')
        await conn.execute('PRAGMA synchronous=NORMAL')
        await conn.execute(f'PRAGMA busy_timeout={config.DB_TIMEOUT * 1000}')
        return conn

    @asynccontextmanager
    async def get_connection(self):
        """BITTA doimiy ulanishni qayta ishlatadi.

        Ilgari har bir amal uchun yangi ulanish ochilardi (bitta guruh xabariga ~4 ta).
        Ko'p guruhda bu "database is locked" xatolarini keltirib chiqaradi.
        Lock ketma-ketlikni ta'minlaydi — SQLite baribir yozishni navbatga qo'yadi.
        DIQQAT: lock qayta kirmaydigan (non-reentrant) — get_connection ichida
        yana get_connection chaqirmang.
        """
        async with self._conn_lock:
            if self._conn is None:
                self._conn = await self._connect()
            try:
                yield self._conn
            except Exception:
                try:
                    await self._conn.rollback()
                except Exception:
                    # Ulanish buzilgan — yopib tashlaymiz, keyingi amalda qaytadan ochiladi
                    try:
                        await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                raise

    async def close(self) -> None:
        async with self._conn_lock:
            if self._conn is not None:
                try:
                    await self._conn.close()
                    logger.info("Baza ulanishi yopildi")
                except Exception as e:
                    logger.warning(f"Baza ulanishini yopishda xato: {e}")
                self._conn = None

    async def backup(self, only_if_older_than: Optional[int] = None) -> Optional[str]:
        """Bazaning nusxasini oladi (VACUUM INTO — bot ishlab turganda ham xavfsiz).

        Oxirgi config.BACKUP_KEEP ta nusxa saqlanadi, qolganlari o'chiriladi.
        only_if_older_than berilsa va oxirgi nusxa shundan yangi bo'lsa — hech narsa
        qilmaydi. Bu bot bir necha marta qayta ishga tushganda kunlik nusxalarning
        yangi fayllar bilan siqib chiqarilishini oldini oladi.
        """
        try:
            os.makedirs(config.BACKUP_DIR, exist_ok=True)

            if only_if_older_than is not None:
                existing = [os.path.join(config.BACKUP_DIR, f)
                            for f in os.listdir(config.BACKUP_DIR)
                            if f.startswith('bot_data-') and f.endswith('.db')]
                if existing:
                    newest = max(os.path.getmtime(p) for p in existing)
                    if time.time() - newest < only_if_older_than:
                        logger.info("Backup o'tkazib yuborildi — yaqinda nusxa olingan")
                        return None
            stamp = get_uz_time().strftime('%Y%m%d-%H%M%S-%f')
            dest = os.path.join(config.BACKUP_DIR, f'bot_data-{stamp}.db')
            if os.path.exists(dest):
                os.remove(dest)

            # VACUUM tranzaksiya ichida ishlamaydi — alohida autocommit ulanish
            async with aiosqlite.connect(self.db_path, timeout=config.DB_TIMEOUT,
                                         isolation_level=None) as conn:
                await conn.execute('VACUUM INTO ?', (dest,))

            files = sorted(f for f in os.listdir(config.BACKUP_DIR)
                           if f.startswith('bot_data-') and f.endswith('.db'))
            for old in (files[:-config.BACKUP_KEEP] if len(files) > config.BACKUP_KEEP else []):
                try:
                    os.remove(os.path.join(config.BACKUP_DIR, old))
                except OSError as e:
                    logger.warning(f"Eski backup o'chmadi ({old}): {e}")

            size_kb = os.path.getsize(dest) // 1024
            logger.info(f"Backup olindi: {dest} ({size_kb} KB)")
            return dest
        except Exception as e:
            logger.error(f"Backup olinmadi: {e}")
            return None

    async def init(self) -> None:
        async with self._init_lock:
            try:
                async with self.get_connection() as conn:
                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY,
                            username TEXT,
                            first_name TEXT,
                            message_count INTEGER DEFAULT 0,
                            warn_count INTEGER DEFAULT 0,
                            is_banned INTEGER DEFAULT 0,
                            started_bot INTEGER DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_username ON users(username)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_banned ON users(is_banned)')

                    # Migratsiya: eski bazalarga started_bot ustunini qo'shamiz.
                    # Faqat botni shaxsiy chatda /start qilgan userlargagina broadcast yuboriladi
                    # (Telegram botga birinchi yozmagan userga xabar yuborishni taqiqlaydi).
                    existing_cols = await (await conn.execute("PRAGMA table_info(users)")).fetchall()
                    if 'started_bot' not in {col[1] for col in existing_cols}:
                        await conn.execute('ALTER TABLE users ADD COLUMN started_bot INTEGER DEFAULT 0')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_started ON users(started_bot)')

                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS groups (
                            chat_id INTEGER PRIMARY KEY,
                            title TEXT NOT NULL,
                            is_active INTEGER DEFAULT 1,
                            member_count INTEGER DEFAULT 0,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_active_groups ON groups(is_active)')

                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS activity_log (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id INTEGER NOT NULL,
                            username TEXT,
                            first_name TEXT,
                            chat_id INTEGER NOT NULL,
                            chat_title TEXT,
                            message_text TEXT,
                            message_type TEXT,
                            action_taken TEXT,
                            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_chat ON activity_log(chat_id)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_log(user_id)')

                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS stats (
                            key TEXT PRIMARY KEY,
                            value INTEGER DEFAULT 0,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')

                    stats_keys = ['total_messages', 'blocked_links', 'banned_users', 'muted_users', 'total_warnings', 'broadcasts_sent']
                    for key in stats_keys:
                        await conn.execute('INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)', (key,))

                    await conn.commit()
            except Exception as e:
                logger.exception(f"Database initialization failed: {e}")
                raise

    async def update_user(self, user_id: int, username: str, first_name: str) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, message_count, warn_count)
                    VALUES (?, ?, ?, 1, 0)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = excluded.username,
                        first_name = excluded.first_name,
                        message_count = message_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                ''', (user_id, username, first_name))
                await conn.execute('UPDATE stats SET value = value + 1, updated_at = CURRENT_TIMESTAMP WHERE key = ?', ('total_messages',))
                await conn.commit()
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")

    async def update_group(self, chat_id: int, title: str, member_count: Optional[int] = None) -> None:
        try:
            async with self.get_connection() as conn:
                # member_count=None bo'lsa mavjud qiymat saqlanadi (COALESCE), aks holda yangilanadi
                await conn.execute('''
                    INSERT INTO groups (chat_id, title, is_active, member_count)
                    VALUES (?, ?, 1, COALESCE(?, 0))
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title = excluded.title,
                        is_active = 1,
                        member_count = COALESCE(?, groups.member_count),
                        updated_at = CURRENT_TIMESTAMP
                ''', (chat_id, title, member_count, member_count))
                await conn.commit()
        except Exception as e:
            logger.error(f"Error updating group {chat_id}: {e}")

    async def deactivate_group(self, chat_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE groups SET is_active = 0, updated_at = CURRENT_TIMESTAMP WHERE chat_id = ?', (chat_id,))
                await conn.commit()
                logger.info(f"Guruh faolsizlantirildi: {chat_id}")
        except Exception as e:
            logger.error(f"deactivate_group({chat_id}) xatosi: {e}")

    async def increment_stat(self, key: str, amount: int = 1) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE stats SET value = value + ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?', (amount, key))
                await conn.commit()
        except Exception as e:
            logger.error(f"increment_stat({key}) xatosi: {e}")

    async def get_active_groups(self):
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('SELECT chat_id, title FROM groups WHERE is_active = 1 ORDER BY title')
                rows = await cursor.fetchall()
                return [(row['chat_id'], row['title']) for row in rows]
        except Exception as e:
            logger.error(f"get_active_groups xatosi: {e}")
            return []

    async def get_all_users(self):
        # OPT-IN ro'yxat: faqat botni shaxsiy chatda OCHGAN (started_bot = 1) odamlar.
        # Guruhda yozib qolgan, lekin botni ochmagan odamlarga broadcast YUBORILMAYDI —
        # bu Telegram'ning so'ralmagan-xabar taqiqiga mos va botni ban xavfidan saqlaydi.
        # started_bot: 1 = obunachi (DM ochgan),  0 = guruh a'zosi (nishon emas),
        #             -1 = bloklagan (broadcast'da Forbidden bo'lgan, ro'yxatdan chiqarilgan).
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('SELECT user_id, username, first_name FROM users WHERE is_banned = 0 AND started_bot = 1 ORDER BY user_id')
                rows = await cursor.fetchall()
                return [(row['user_id'], row['username'], row['first_name']) for row in rows]
        except Exception as e:
            logger.error(f"get_all_users xatosi: {e}")
            return []

    async def set_reachable(self, user_id: int, reachable: bool) -> None:
        """Broadcast natijasiga ko'ra userni 'yetadi' (1) yoki 'o'lik' (-1) deb belgilaydi."""
        try:
            async with self.get_connection() as conn:
                await conn.execute(
                    'UPDATE users SET started_bot = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?',
                    (1 if reachable else -1, user_id)
                )
                await conn.commit()
        except Exception as e:
            logger.error(f"set_reachable({user_id}) xatosi: {e}")

    async def mark_user_started(self, user_id: int, username: str, first_name: str) -> None:
        """Foydalanuvchi botni shaxsiy chatda ochganini belgilaydi (broadcast uchun yetib boradigan)."""
        try:
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, message_count, warn_count, started_bot)
                    VALUES (?, ?, ?, 0, 0, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        started_bot = 1,
                        username = excluded.username,
                        first_name = excluded.first_name,
                        updated_at = CURRENT_TIMESTAMP
                ''', (user_id, username, first_name))
                await conn.commit()
        except Exception as e:
            logger.error(f"Error marking user {user_id} started: {e}")

    async def add_warning(self, user_id: int, username: str, first_name: str) -> int:
        try:
            async with self.get_connection() as conn:
                # Atomik increment: SELECT+UPDATE race condition oldini olinadi.
                # RETURNING bilan yangi qiymatni bir so'rovda olamiz (SQLite 3.35+)
                cursor = await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, message_count, warn_count)
                    VALUES (?, ?, ?, 0, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        warn_count = users.warn_count + 1,
                        username = excluded.username,
                        first_name = excluded.first_name,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING warn_count
                ''', (user_id, username, first_name))
                row = await cursor.fetchone()
                warn_count = row['warn_count'] if row else 1

                await conn.execute('UPDATE stats SET value = value + 1, updated_at = CURRENT_TIMESTAMP WHERE key = ?', ('total_warnings',))
                await conn.commit()
                return warn_count
        except Exception as e:
            logger.error(f"Error adding warning for user {user_id}: {e}")
            return 0

    async def ban_user(self, user_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE users SET is_banned = 1, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"ban_user({user_id}) xatosi: {e}")

    async def unban_user(self, user_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE users SET is_banned = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"unban_user({user_id}) xatosi: {e}")

    async def reset_warnings(self, user_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE users SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"reset_warnings({user_id}) xatosi: {e}")

    async def get_stats(self):
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('SELECT COUNT(*) as count FROM users')
                users_count = (await cursor.fetchone())['count']

                cursor = await conn.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1')
                groups_count = (await cursor.fetchone())['count']

                cursor = await conn.execute('SELECT key, value FROM stats')
                stats_rows = await cursor.fetchall()
                stats = {row['key']: row['value'] for row in stats_rows}

                return {'users': users_count, 'groups': groups_count, **stats}
        except Exception as e:
            logger.error(f"get_stats xatosi: {e}")
            return {'users': 0, 'groups': 0, 'blocked_links': 0, 'banned_users': 0, 'total_messages': 0, 'muted_users': 0, 'total_warnings': 0, 'broadcasts_sent': 0}

    async def log_activity(self, user_id, username, first_name, chat_id, chat_title, message_text, message_type, action_taken):
        try:
            # Maxfiylik: oddiy xabarlar matni SAQLANMAYDI.
            # Faqat spam/flood xabarlari dalil sifatida yoziladi
            # (LOG_MESSAGE_TEXT=true bo'lsa hammasi yoziladi).
            keep_text = config.LOG_MESSAGE_TEXT or message_type in ('spam_link', 'flood')
            truncated_text = (message_text or "")[:500] if keep_text else ""
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO activity_log
                    (user_id, username, first_name, chat_id, chat_title, message_text, message_type, action_taken)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, username, first_name, chat_id, chat_title, truncated_text, message_type, action_taken))
                await conn.commit()
        except Exception as e:
            logger.error(f"log_activity xatosi: {e}")

    async def get_stat(self, key: str, default: int = 0) -> int:
        try:
            async with self.get_connection() as conn:
                row = await (await conn.execute('SELECT value FROM stats WHERE key = ?', (key,))).fetchone()
                return row['value'] if row else default
        except Exception as e:
            logger.error(f"get_stat({key}) xatosi: {e}")
            return default

    async def set_stat(self, key: str, value: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO stats (key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                ''', (key, value))
                await conn.commit()
        except Exception as e:
            logger.error(f"set_stat({key}) xatosi: {e}")

    async def get_recent_activity(self, hours: int = 1, limit: int = 50):
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('''
                    SELECT * FROM activity_log
                    WHERE timestamp > datetime('now', ? || ' hours')
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (f'-{hours}', limit))
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"get_recent_activity xatosi: {e}")
            return []

    async def cleanup_old_activity(self, days: int = 7) -> int:
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('''
                    DELETE FROM activity_log
                    WHERE timestamp < datetime('now', ? || ' days')
                ''', (f'-{days}',))
                deleted = cursor.rowcount
                await conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"cleanup_old_activity xatosi: {e}")
            return 0


db = Database(config.DB_FILE)
def get_user_mention(user) -> str:
    """Markdown-xavfsiz mention. Username bo'lsa @username, aks holda bosiladigan havola."""
    if user.username:
        return f"@{escape_markdown(user.username)}"
    first_name = user.first_name or "Foydalanuvchi"
    # username yo'q bo'lsa — bosiladigan inline mention havolasi
    return f"[{escape_markdown(first_name)}](tg://user?id={user.id})"


def get_uz_time() -> datetime:
    return datetime.now(UZ_TZ)


def format_uz_time(dt: datetime = None, format_str: str = '%d.%m.%Y %H:%M:%S') -> str:
    if dt is None:
        dt = get_uz_time()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo('UTC')).astimezone(UZ_TZ)
    else:
        dt = dt.astimezone(UZ_TZ)

    return dt.strftime(format_str)


# @username: 5-32 belgi. Oldida harf/raqam/nuqta bo'lmasligi -> email ichidagi @ tutilmaydi.
MENTION_RE = re.compile(r'(?<![a-zA-Z0-9_@./])@([a-zA-Z][a-zA-Z0-9_]{4,31})\b')

# Reklama konteksti: @mention FAQAT shu so'zlar bilan birga kelganda spam hisoblanadi.
# Shunday qilib guruhda odamlar bir-birini "@ali" deb chaqirsa jazolanmaydi.
PROMO_CONTEXT_RE = re.compile(
    r'(kanal|kanalim|kanalga|obuna|subscri|join\s|guruhim|guruhga\s*kir|'
    r'reklama|aksiya|chegirma|promo\s?kod|bepul\s*(kurs|darslik|kitob)|'
    r'pul\s*ishla|daromad|investitsiya|kazino|casino|stavka|bukmeker|'
    r'yozila|buyurtma|zakaz|sotiladi|arzon\s*narx)',
    re.IGNORECASE
)


def _is_promo_mention(text: str) -> bool:
    """@mention reklamaga o'xshaydimi.

    MUHIM: ilgari har qanday @username (5+ belgi) reklama deb bloklanardi —
    "Salom @Shahriyor qalaysan" ham ban olib kelardi. Endi faqat:
      - reklama konteksti bo'lsa (kanal/obuna/reklama/...), yoki
      - bitta xabarda 3+ mention bo'lsa (mention-spam), yoki
      - BLOCK_MENTIONS=true qo'yilgan bo'lsa.
    """
    mentions = [m for m in MENTION_RE.findall(text)
                if m.lower() != (config.BOT_USERNAME or '').lower()]
    if not mentions:
        return False
    if config.BLOCK_MENTIONS:
        return True
    if len(mentions) >= 3:
        return True
    return bool(PROMO_CONTEXT_RE.search(text))


def contains_link(text: str) -> bool:
    if not text:
        return False
    if any(pattern.search(text) for pattern in config.LINK_PATTERNS):
        return True
    return _is_promo_mention(text)


def _schedule_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int) -> None:
    """Xabarni delay sekunddan keyin o'chirish. job_queue yo'q bo'lsa ham qulamaydi."""
    job_queue = getattr(context, 'job_queue', None)
    if job_queue is None:
        logger.warning("job_queue mavjud emas — xabar avtomatik o'chirilmaydi")
        return
    job_queue.run_once(delete_message_job, delay, data={'chat_id': chat_id, 'message_id': message_id})


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await context.bot.delete_message(chat_id=context.job.data['chat_id'], message_id=context.job.data['message_id'])
    except TelegramError:
        pass


# Guruh ma'lumotini yangilash throttle keshi: {chat_id: last_member_count_fetch_ts}
_group_refresh: Dict[int, float] = {}
_GROUP_REFRESH_INTERVAL = 3600  # 1 soat

# Adminga "yangi xabar" bildirishnomasi throttle keshi: {user_id: last_notify_ts}
_pm_notify_ts: Dict[int, float] = {}
_PM_NOTIFY_INTERVAL = 300  # har userdan info-header eng ko'pi 5 daqiqada bir marta

# Shaxsiy chatda flood ogohlantirishi throttle keshi: {user_id: last_warn_ts}
_pm_flood_warn_ts: Dict[int, float] = {}
_PM_FLOOD_WARN_INTERVAL = 60

# Kesh yoshi: eski yozuvlar tozalanadi (xotira oqishini oldini olish)
_PM_CACHE_TTL = 86400

# Kritik xato haqida adminga ogohlantirish throttle'i (restart loop'da spam bo'lmasligi uchun)
_critical_alert_ts = 0.0
_CRITICAL_ALERT_INTERVAL = 600


def _prune_pm_caches() -> None:
    """_pm_notify_ts va _pm_flood_warn_ts keshlarini tozalaydi.

    Ilgari _pm_notify_ts HECH QACHON tozalanmasdi — botga yozgan har bir odam
    xotirada abadiy qolardi (sekin xotira oqishi).
    """
    now = asyncio.get_event_loop().time()
    removed = 0
    for cache in (_pm_notify_ts, _pm_flood_warn_ts):
        for uid, ts in list(cache.items()):
            if now - ts > _PM_CACHE_TTL:
                del cache[uid]
                removed += 1
    if removed:
        logger.debug(f"PM keshidan {removed} ta eski yozuv tozalandi")


# Guruhga bog'langan kanal keshi: {chat_id: (ts, linked_chat_id|None)}
_linked_chat: Dict[int, tuple] = {}
_LINKED_CHAT_TTL = 3600


async def _get_linked_chat_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Optional[int]:
    """Guruhga bog'langan kanal ID si (muhokama guruhi uchun). Soatiga bir marta so'raladi."""
    now = asyncio.get_event_loop().time()
    cached = _linked_chat.get(chat_id)
    if cached and now - cached[0] < _LINKED_CHAT_TTL:
        return cached[1]
    try:
        info = await context.bot.get_chat(chat_id)
        linked = getattr(info, 'linked_chat_id', None)
    except TelegramError as e:
        logger.debug(f"get_chat({chat_id}) xatosi: {e}")
        # Xatoda keshni yangilamaymiz — keyingi xabarda qayta urinamiz
        return cached[1] if cached else None
    _linked_chat[chat_id] = (now, linked)
    if linked:
        logger.info(f"Guruh {chat_id} ga bog'langan kanal: {linked}")
    return linked


async def _check_sender_chat(context: ContextTypes.DEFAULT_TYPE, message) -> Optional[Tuple[bool, str]]:
    """Xabar KANAL yoki GURUH nomidan yuborilganmi va unga ruxsat bormi.

    Telegram'da bunday xabarlarda `from_user` haqiqiy odam emas — xizmat akkaunti
    (GroupAnonymousBot yoki Channel_Bot). Shuning uchun oddiy admin tekshiruvi
    ishlamaydi: get_chat_member o'sha soxta ID ni "a'zo emas" deb qaytaradi va
    guruh egasining o'z posti ham reklama deb o'chirib yuborilardi.

    None -> sender_chat yo'q, oddiy tekshiruv davom etsin
    (True, sabab)  -> ruxsat berildi
    (False, sabab) -> begona kanal
    """
    sender_chat = getattr(message, 'sender_chat', None)
    if sender_chat is None:
        return None

    chat = message.chat

    # 1. Guruhning O'ZI nomidan — ya'ni anonim admin ("Remain anonymous")
    if sender_chat.id == chat.id:
        return (True, "anonim_admin")

    # 2. Bog'langan kanalning posti avtomatik forward bo'lgan (muhokama guruhi)
    if getattr(message, 'is_automatic_forward', False):
        return (True, "boglangan_kanal_posti")

    # 3. ALLOWED_CHANNELS ro'yxatidagi kanal
    if config.is_allowed_channel(sender_chat):
        return (True, "ruxsat_berilgan_kanal")

    # 4. Guruhga bog'langan kanal nomidan yozilgan
    linked_id = await _get_linked_chat_id(context, chat.id)
    if linked_id and sender_chat.id == linked_id:
        return (True, "boglangan_kanal")

    return (False, "begona_kanal")


async def _handle_foreign_channel_message(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    """Begona kanal nomidan yozilgan xabar.

    Kanalga ogohlantirish berib yoki uni ban qilib bo'lmaydi (u odam emas),
    shuning uchun faqat reklama bo'lsa xabar o'chiriladi.
    """
    chat = message.chat
    sender_chat = message.sender_chat
    text = message.text or message.caption or ""

    if not contains_link(text):
        return

    logger.warning(
        f"🚫 KANAL REKLAMASI: sender_chat={sender_chat.id} "
        f"(@{sender_chat.username or '—'}) chat={chat.id}"
    )

    try:
        await message.delete()
    except TelegramError as e:
        logger.warning(f"Kanal xabarini o'chirib bo'lmadi: {e}")
        return

    await db.increment_stat('blocked_links')
    await db.log_activity(sender_chat.id, sender_chat.username, sender_chat.title,
                          chat.id, chat.title or "Unknown", text, "spam_link", "blocked_channel")

    try:
        name = escape_markdown(sender_chat.title or sender_chat.username or "Kanal")
        # message o'chirilgani uchun reply emas, oddiy xabar yuboramiz
        warning_msg = await context.bot.send_message(
            chat_id=chat.id,
            text=f"🚫 **{name}** kanali nomidan yuborilgan reklama o'chirildi.",
            parse_mode='Markdown'
        )
        _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id, config.WARNING_DELETE_DELAY)
    except TelegramError as e:
        logger.error(f"Kanal ogohlantirishini yuborib bo'lmadi: {e}")


async def _maybe_update_group(context: ContextTypes.DEFAULT_TYPE, chat) -> None:
    """Guruhni DB ga yozadi. member_count uchun API ni faqat soatiga bir marta chaqiradi."""
    now = asyncio.get_event_loop().time()
    last = _group_refresh.get(chat.id, 0)

    if now - last > _GROUP_REFRESH_INTERVAL:
        _group_refresh[chat.id] = now
        try:
            member_count = await context.bot.get_chat_member_count(chat.id)
            await db.update_group(chat.id, chat.title or "Unknown", member_count)
            return
        except TelegramError as e:
            # Xato bo'lsa bir soat kutmaymiz — 5 daqiqadan keyin qayta urinamiz
            _group_refresh[chat.id] = now - _GROUP_REFRESH_INTERVAL + 300
            logger.debug(f"get_chat_member_count({chat.id}) xatosi: {e}")

    # Tez-tez: API siz, faqat title yangilanadi (mavjud member_count saqlanadi)
    await db.update_group(chat.id, chat.title or "Unknown")


@error_handler
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    message = update.message
    user = message.from_user

    if message.chat.type == 'private':
        is_new_user = False
        try:
            async with db.get_connection() as conn:
                existing_user = await (await conn.execute('SELECT user_id FROM users WHERE user_id = ?', (user.id,))).fetchone()
                is_new_user = existing_user is None
        except Exception as e:
            logger.error(f"Error checking if user is new: {e}")
            is_new_user = False

        await db.update_user(user.id, user.username, user.first_name)
        # Shaxsiy chatda /start bosgan — broadcast yetib boradigan user deb belgilaymiz
        await db.mark_user_started(user.id, user.username, user.first_name)

        if is_new_user:
            celebration_text = (
                f"🎉 **TABRIKLAYMIZ, {escape_markdown(user.first_name)}!**\n\n"
                f"🎊 Siz botimizning yangi foydalanuvchisisiz!\n\n"
                f"✨ Botdan foydalanganingiz uchun tashakkur!\n"
                f"🛡 Endi guruhlaringizni himoya qilishingiz mumkin."
            )

            try:
                await message.reply_text(celebration_text, parse_mode='Markdown')
                await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Failed to send celebration message: {e}")

            try:
                username_display = f"@{escape_markdown(user.username)}" if user.username else escape_markdown(user.first_name)
                admin_notification = (
                    f"🎉 **YANGI FOYDALANUVCHI!**\n\n"
                    f"👤 Ism: {escape_markdown(user.first_name)}\n"
                    f"🆔 Username: {username_display}\n"
                    f"🔢 ID: `{user.id}`\n"
                    f"⏰ {format_uz_time()}\n\n"
                    f"🎊 Bot foydalanuvchilari yana 1 taga ko'paydi!"
                )

                await context.bot.send_message(chat_id=config.ADMIN_ID, text=admin_notification, parse_mode='Markdown')
                logger.info(f"✅ New user notification: {user.id}")
            except Exception as e:
                logger.error(f"Failed to send notification: {e}")

        welcome_text = (
            f"👋 Assalomu alaykum, **{escape_markdown(user.first_name)}**!\n\n"
            "🛡 Men guruhlarni spam va reklamadan himoya qiluvchi botman.\n\n"
            "**Imkoniyatlar:**\n"
            "• 🔗 Link va reklama bloklash\n"
            "• ⚠️ Avtomatik ogohlantirish\n"
            "• 🔇 Spam nazorati"
        )

        keyboard = [
            [InlineKeyboardButton("➕ Guruhga qo'shish", url=f"https://t.me/{config.BOT_USERNAME}?startgroup=true")],
            [InlineKeyboardButton("ℹ️ Yordam", callback_data="help")]
        ]

        if is_bot_admin(user.id):
            keyboard.append([InlineKeyboardButton("📊 Admin Panel", callback_data="admin")])

        await message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await message.reply_text("✅ Bot faol ishlayapti!\n🛡 Guruh spam va reklamadan himoyalangan.", parse_mode='Markdown')


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if query:
        await query.answer()
        help_text = "📞 ALOQA\n\nBotda muammo yuzaga kelsa, quyidagi tugmani bosing:"
        keyboard = [
            [InlineKeyboardButton("💬 Shahriyor Umaraliyev", url=f"tg://user?id={config.ADMIN_ID}")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="back")]
        ]

        try:
            await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except TelegramError as e:
            logger.error(f"Error in help_handler: {e}")
    else:
        if update.message:
            help_text = "📞 ALOQA\n\nBotda muammo yuzaga kelsa, quyidagi tugmani bosing:"
            keyboard = [[InlineKeyboardButton("💬 Shahriyor Umaraliyev", url=f"tg://user?id={config.ADMIN_ID}")]]
            await update.message.reply_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
@error_handler
async def admin_panel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 Statistika", callback_data="stats")],
        [
            InlineKeyboardButton("📋 Guruhlar", callback_data="groups_list"),
            InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="users_count")
        ],
        [InlineKeyboardButton("📢 Xabar yuborish", callback_data="broadcast_menu")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="back")]
    ]

    await query.edit_message_text("🔧 **ADMIN PANEL**\n\nQuyidagi bo'limlardan birini tanlang:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only
@error_handler
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    stats = await db.get_stats()
    flood_stats = flood_control.get_stats()

    stats_text = (
        "📊 **BOT STATISTIKASI**\n\n"
        "👥 **Foydalanuvchilar:**\n"
        f"   • Jami: **{stats['users']:,}** ta\n"
        f"   • Banlangan: **{stats.get('banned_users', 0):,}** ta\n\n"
        "💬 **Guruhlar:**\n"
        f"   • Faol: **{stats['groups']:,}** ta\n\n"
        "📨 **Xabarlar:**\n"
        f"   • Jami: **{stats['total_messages']:,}** ta\n"
        f"   • Bloklangan linklar: **{stats.get('blocked_links', 0):,}** ta\n\n"
        "⚠️ **Ogohlantirishlar:**\n"
        f"   • Jami: **{stats.get('total_warnings', 0):,}** ta\n"
        f"   • Mute qilingan: **{stats.get('muted_users', 0):,}** ta\n\n"
        "📢 **Broadcast:**\n"
        f"   • Yuborilgan: **{stats.get('broadcasts_sent', 0):,}** ta\n\n"
        "🔄 **Flood Control:**\n"
        f"   • Kuzatilayotgan: **{flood_stats['tracked_users']}** user\n\n"
        f"🕐 Yangilangan: {format_uz_time()}"
    )

    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]
    await query.edit_message_text(stats_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only
@error_handler
async def groups_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    groups = await db.get_active_groups()

    if not groups:
        text = "📋 **GURUHLAR RO'YXATI**\n\n❌ Hozircha faol guruhlar yo'q."
    else:
        groups_text = "\n".join([f"{i}. {title}" for i, (_, title) in enumerate(groups[:25], 1)])
        text = f"📋 **GURUHLAR RO'YXATI**\n\nJami: **{len(groups)}** ta faol guruh\n\n{groups_text}"
        if len(groups) > 25:
            text += f"\n\n... va yana **{len(groups) - 25}** ta guruh"

    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only
@error_handler
async def users_count_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        async with db.get_connection() as conn:
            cursor = await conn.execute('SELECT COUNT(*) as count FROM users')
            row = await cursor.fetchone()
            total_users = row['count'] if row else 0

            cursor = await conn.execute('''
                SELECT user_id, username, first_name, message_count, warn_count, is_banned, updated_at
                FROM users ORDER BY updated_at DESC LIMIT 10
            ''')
            users = await cursor.fetchall()

        if total_users == 0:
            text = "👥 **FOYDALANUVCHILAR**\n\nHali hech kim yo'q."
        else:
            text = f"👥 **FOYDALANUVCHILAR** ({total_users:,} ta)\n\n"

            for i, user in enumerate(users, 1):
                username = user['username'] or user['first_name'] or "Unknown"
                username_escaped = escape_markdown(username)
                msg_count = user['message_count']
                banned = "🚫" if user['is_banned'] else ""

                text += f"{i}. {username_escaped} {banned}\n   💬 {msg_count} xabar\n"

            if total_users > 10:
                text += f"\n... va yana {total_users - 10:,} ta\n"

            text += f"\n💡 To'liq ro'yxat: /users"

        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in users_count_handler: {e}")
        await query.edit_message_text("❌ Xatolik yuz berdi.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orqaga", callback_data="admin")]]), parse_mode='Markdown')


@admin_only
@error_handler
async def broadcast_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    groups = await db.get_active_groups()
    users = await db.get_all_users()

    keyboard = [
        [InlineKeyboardButton(f"📢 Guruhlarga ({len(groups)} ta)", callback_data="broadcast_groups")],
        [InlineKeyboardButton(f"👥 Foydalanuvchilarga ({len(users)} ta)", callback_data="broadcast_users")],
        [InlineKeyboardButton(f"🌐 Barchaga ({len(groups) + len(users)} ta)", callback_data="broadcast_all")],
        [InlineKeyboardButton("❌ Bekor qilish", callback_data="admin")]
    ]

    await query.edit_message_text("📢 **XABAR YUBORISH**\n\nXabarni qayerga yuborishni tanlang:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')


@admin_only
@error_handler
async def broadcast_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    groups = await db.get_active_groups()
    keyboard = [[InlineKeyboardButton("❌ Bekor qilish", callback_data="admin")]]

    await query.edit_message_text(
        f"📢 **GURUHLARGA XABAR YUBORISH**\n\n"
        f"📊 Faol guruhlar: **{len(groups)}** ta\n\n"
        f"📝 Endi menga **shaxsiy xabar** sifatida yubormoqchi bo'lgan xabaringizni yuboring.\n\n"
        f"ℹ️ Rasm, video, audio va boshqa media ham qo'llab-quvvatlanadi.\n\n"
        f"❌ Bekor qilish: /cancel",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    context.user_data['broadcast_mode'] = 'groups'


@admin_only
@error_handler
async def broadcast_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    users = await db.get_all_users()
    keyboard = [[InlineKeyboardButton("❌ Bekor qilish", callback_data="admin")]]

    await query.edit_message_text(
        f"👥 **FOYDALANUVCHILARGA XABAR YUBORISH**\n\n"
        f"📊 Foydalanuvchilar: **{len(users)}** ta\n\n"
        f"📝 Endi menga **shaxsiy xabar** sifatida yubormoqchi bo'lgan xabaringizni yuboring.\n\n"
        f"ℹ️ Rasm, video, audio va boshqa media ham qo'llab-quvvatlanadi.\n\n"
        f"❌ Bekor qilish: /cancel",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    context.user_data['broadcast_mode'] = 'users'


@admin_only
@error_handler
async def broadcast_all_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    groups = await db.get_active_groups()
    users = await db.get_all_users()
    total = len(groups) + len(users)

    keyboard = [[InlineKeyboardButton("❌ Bekor qilish", callback_data="admin")]]

    await query.edit_message_text(
        f"🌐 **HAMMAGA XABAR YUBORISH**\n\n"
        f"📊 Jami: **{total}** ta (guruhlar + foydalanuvchilar)\n"
        f"   • Guruhlar: **{len(groups)}** ta\n"
        f"   • Foydalanuvchilar: **{len(users)}** ta\n\n"
        f"📝 Endi menga **shaxsiy xabar** sifatida yubormoqchi bo'lgan xabaringizni yuboring.\n\n"
        f"ℹ️ Rasm, video, audio va boshqa media ham qo'llab-quvvatlanadi.\n\n"
        f"❌ Bekor qilish: /cancel",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

    context.user_data['broadcast_mode'] = 'all'

@error_handler
async def back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    context.user_data['broadcast_mode'] = None

    user = query.from_user
    welcome_text = (
        f"👋 Assalomu alaykum, **{escape_markdown(user.first_name)}**!\n\n"
        "🛡 Men guruhlarni spam va reklamadan himoya qiluvchi botman.\n\n"
        "**Imkoniyatlar:**\n"
        "• 🔗 Link va reklama bloklash\n"
        "• ⚠️ Avtomatik ogohlantirish\n"
        "• 🔇 Spam nazorati"
    )

    keyboard = [
        [InlineKeyboardButton("➕ Guruhga qo'shish", url=f"https://t.me/{config.BOT_USERNAME}?startgroup=true")],
        [InlineKeyboardButton("ℹ️ Yordam", callback_data="help")]
    ]

    if is_bot_admin(user.id):
        keyboard.append([InlineKeyboardButton("📊 Admin Panel", callback_data="admin")])

    try:
        await query.edit_message_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except TelegramError as e:
        logger.error(f"Error in back_handler: {e}")


@admin_only
@error_handler
async def activity_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    args = context.args
    hours = 1
    if args and args[0].isdigit():
        hours = min(int(args[0]), 24)

    activities = await db.get_recent_activity(hours=hours, limit=50)

    if not activities:
        await update.message.reply_text(f"📊 **ACTIVITY LOG**\n\nOxirgi {hours} soatda faoliyat yo'q.", parse_mode='Markdown')
        return

    text = f"📊 **ACTIVITY LOG** (Oxirgi {hours} soat)\n\n"

    for i, act in enumerate(activities[:20], 1):
        username = act['username'] or act['first_name'] or "Unknown"
        action = act['action_taken']
        raw_text = act['message_text'] or "—"
        msg_preview = (raw_text[:30] + "...") if len(raw_text) > 30 else raw_text
        try:
            ts = act['timestamp']
            dt_utc = ts if isinstance(ts, datetime) else datetime.strptime(str(ts).split('.')[0], '%Y-%m-%d %H:%M:%S')
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=ZoneInfo('UTC'))
            dt_uz = dt_utc.astimezone(UZ_TZ)
            timestamp = dt_uz.strftime('%d.%m.%Y %H:%M:%S')
        except Exception:
            timestamp = str(act['timestamp']).split('.')[0] if '.' in str(act['timestamp']) else str(act['timestamp'])

        text += f"{i}. **{username}** | {act['chat_title']}\n   💬 {msg_preview}\n   ⚡ {action} | {timestamp}\n\n"

    if len(activities) > 20:
        text += f"... va yana {len(activities) - 20} ta faoliyat\n\n"

    text += f"📍 Jami: **{len(activities)}** ta faoliyat\n⏰ Yangilangan: {format_uz_time(format_str='%H:%M:%S')}"

    await update.message.reply_text(text, parse_mode='Markdown')


@admin_only
@error_handler
async def users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    try:
        async with db.get_connection() as conn:
            cursor = await conn.execute('''
                SELECT COUNT(*) as total_users, SUM(message_count) as total_messages, SUM(is_banned) as banned_count 
                FROM users
            ''')
            stats_row = await cursor.fetchone()
            total_users = stats_row['total_users'] if stats_row else 0
            total_messages = stats_row['total_messages'] if stats_row and stats_row['total_messages'] else 0
            banned_count = stats_row['banned_count'] if stats_row and stats_row['banned_count'] else 0

            cursor = await conn.execute('''
                SELECT user_id, username, first_name, message_count, warn_count, is_banned, created_at, updated_at
                FROM users ORDER BY updated_at DESC LIMIT 30
            ''')
            users = await cursor.fetchall()

        if total_users == 0:
            await update.message.reply_text("📊 **FOYDALANUVCHILAR**\n\nHali hech kim botdan foydalanmagan.", parse_mode='Markdown')
            return

        text = f"👥 **FOYDALANUVCHILAR** ({total_users} ta)\n\n"

        for i, user in enumerate(users, 1):
            username = user['username'] or user['first_name'] or "Unknown"
            msg_count = user['message_count']
            warns = user['warn_count']
            banned = "🚫" if user['is_banned'] else ""

            try:
                ts = user['updated_at']
                dt_utc = ts if isinstance(ts, datetime) else datetime.strptime(str(ts).split('.')[0], '%Y-%m-%d %H:%M:%S')
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=ZoneInfo('UTC'))
                dt_uz = dt_utc.astimezone(UZ_TZ)
                updated = dt_uz.strftime('%d.%m.%Y')
            except Exception:
                updated = str(user['updated_at']).split(' ')[0] if user['updated_at'] and ' ' in str(user['updated_at']) else (str(user['updated_at']) or "Unknown")

            username_escaped = escape_markdown(username)
            text += f"{i}. **{username_escaped}** {banned}\n   💬 {msg_count} xabar | ⚠️ {warns} ogohlantirish\n   📅 {updated}\n\n"

        if total_users > 30:
            text += f"... va yana {total_users - 30} ta foydalanuvchi\n\n"

        text += f"📊 **STATISTIKA:**\n• Jami foydalanuvchilar: {total_users:,}\n• Jami xabarlar: {total_messages:,}\n• Ban qilinganlar: {banned_count:,}\n⏰ {format_uz_time(format_str='%H:%M:%S')}"

        keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error in users_handler: {e}")
        await update.message.reply_text("❌ Xatolik yuz berdi. Qayta urinib ko'ring.", parse_mode='Markdown')


@admin_only
@error_handler
async def refresh_admins_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    try:
        status_msg = await update.message.reply_text("🔄 Admin ro'yxatlari yangilanmoqda...", parse_mode='Markdown')

        groups = await db.get_active_groups()
        success_count = 0

        for chat_id, title in groups:
            try:
                await admin_manager.refresh_admins(context.bot, chat_id)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to refresh admins for {title}: {e}")

        stats = admin_manager.get_stats()

        result_text = (
            f"✅ **ADMIN RO'YXATLARI YANGILANDI**\n\n"
            f"📊 Guruhlar: {success_count}/{len(groups)}\n"
            f"👥 Jami adminlar: {stats['total_admins']}\n\n"
            f"ℹ️ Endi barcha adminlar to'g'ri taniladi."
        )

        await status_msg.edit_text(result_text, parse_mode='Markdown')
        logger.info(f"Manual admin refresh completed: {success_count}/{len(groups)} groups")

    except Exception as e:
        logger.error(f"Error in refresh_admins_handler: {e}")
        await update.message.reply_text("❌ Xatolik yuz berdi.", parse_mode='Markdown')


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[int], Optional[str]]:
    """Moderatsiya komandasi nishonini aniqlaydi: reply qilingan user yoki argument (ID/@username)."""
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, get_user_mention(u)
    if context.args:
        arg = context.args[0].lstrip('@')
        if arg.isdigit():
            return int(arg), f"`{arg}`"
        try:
            async with db.get_connection() as conn:
                row = await (await conn.execute('SELECT user_id FROM users WHERE username = ?', (arg,))).fetchone()
                if row:
                    return row['user_id'], f"@{escape_markdown(arg)}"
        except Exception as e:
            logger.error(f"_resolve_target username qidirish xatosi: {e}")
    return None, None


async def _moderation_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Komanda guruhda va admin tomonidan chaqirilganini tekshiradi."""
    message = update.effective_message
    if not message or message.chat.type == 'private' or not message.from_user:
        return False
    is_admin, _ = await is_definitely_admin(context, message.chat.id, message.from_user.id, message.from_user)
    if not is_admin:
        await message.reply_text("❌ Bu komanda faqat adminlar uchun.")
        return False
    return True


@error_handler
async def unwarn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await update.effective_message.reply_text("ℹ️ Foydalanish: xabarga **reply** qilib `/unwarn` yoki `/unwarn <ID|@username>`", parse_mode='Markdown')
        return
    await db.reset_warnings(target_id)
    await update.effective_message.reply_text(f"✅ {mention} — ogohlantirishlari tozalandi.", parse_mode='Markdown')


@error_handler
async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await update.effective_message.reply_text("ℹ️ Foydalanish: `/unban <ID|@username>` yoki xabarga reply qiling", parse_mode='Markdown')
        return
    chat = update.effective_message.chat
    try:
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
    except TelegramError as e:
        logger.error(f"unban failed: {e}")
    await db.unban_user(target_id)
    await db.reset_warnings(target_id)
    await update.effective_message.reply_text(f"✅ {mention} — bani olib tashlandi.", parse_mode='Markdown')


@error_handler
async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await update.effective_message.reply_text("ℹ️ Foydalanish: xabarga **reply** qilib `/unmute` yoki `/unmute <ID|@username>`", parse_mode='Markdown')
        return
    chat = update.effective_message.chat
    try:
        await context.bot.restrict_chat_member(
            chat.id, target_id,
            ChatPermissions(can_send_messages=True, can_send_polls=True,
                            can_send_other_messages=True, can_add_web_page_previews=True)
        )
    except TelegramError as e:
        logger.error(f"unmute failed: {e}")
        await update.effective_message.reply_text("❌ Mute'ni ololmadim (bot huquqlarini tekshiring).")
        return
    await db.reset_warnings(target_id)
    await update.effective_message.reply_text(f"✅ {mention} — mute olib tashlandi.", parse_mode='Markdown')


@error_handler
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_bot_admin(update.message.from_user.id):
        return

    if context.user_data.get('broadcast_mode'):
        context.user_data['broadcast_mode'] = None
        await update.message.reply_text("❌ Amal bekor qilindi.", parse_mode='Markdown')


@error_handler
async def private_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user = update.message.from_user
    message = update.message

    # FLOOD HIMOYASI (shaxsiy chat). Ilgari bu yerda hech qanday limit yo'q edi:
    # istalgan odam adminning shaxsiy chatini minglab xabar bilan to'ldirib,
    # botni Telegram rate-limitiga uchratishi mumkin edi (bu paytda guruhlarda ham ishlamaydi).
    if not is_bot_admin(user.id) and await pm_flood_control.check(user.id):
        now = asyncio.get_event_loop().time()
        if now - _pm_flood_warn_ts.get(user.id, 0) > _PM_FLOOD_WARN_INTERVAL:
            _pm_flood_warn_ts[user.id] = now
            try:
                await message.reply_text("⏳ Juda tez yozyapsiz. Iltimos sekinroq — aks holda xabarlaringiz yetib bormaydi.")
            except TelegramError:
                pass
        logger.warning(f"🚫 PM FLOOD: user {user.id} xabari adminga yuborilmadi")
        return

    await db.update_user(user.id, user.username, user.first_name)
    # Shaxsiy chatga yozgan — botni ochgan, broadcast yetib boradigan user
    await db.mark_user_started(user.id, user.username, user.first_name)

    if not is_bot_admin(user.id):
        try:
            # Info-header (kim yozayotgani) faqat 5 daqiqada bir marta — admin chati spamlanmaydi.
            # Xabarning o'zi har doim forward qilinadi.
            now = asyncio.get_event_loop().time()
            if now - _pm_notify_ts.get(user.id, 0) > _PM_NOTIFY_INTERVAL:
                _pm_notify_ts[user.id] = now
                username = user.username or user.first_name or "Unknown"
                user_info = f"📨 YANGI XABAR\n\n👤 User: {username}\n🆔 ID: {user.id}\n⏰ {format_uz_time()}"
                await context.bot.send_message(chat_id=config.ADMIN_ID, text=user_info)

            await message.forward(config.ADMIN_ID)
            logger.info(f"Message from user {user.id} forwarded to admin")
        except TelegramError as e:
            logger.error(f"Error forwarding message to admin: {e}")

        return

    broadcast_mode = context.user_data.get('broadcast_mode')

    if not broadcast_mode:
        return

    # Bir vaqtning o'zida ikkita broadcast ketishini oldini olamiz
    if context.user_data.get('broadcast_running'):
        await update.message.reply_text("⏳ Avvalgi xabar yuborilmoqda, tugashini kuting.", parse_mode='Markdown')
        return

    context.user_data['broadcast_mode'] = None
    context.user_data['broadcast_running'] = True
    asyncio.create_task(run_broadcast_task(context.bot, update.message.chat_id, update.message.message_id, broadcast_mode, context.user_data))

async def run_broadcast_task(bot, from_chat_id, message_id, broadcast_mode, user_data):
    try:
        await _run_broadcast(bot, from_chat_id, message_id, broadcast_mode)
    except Exception as e:
        logger.exception(f"Broadcast task failed: {e}")
        try:
            await bot.send_message(chat_id=from_chat_id, text="❌ Broadcast vaqtida xatolik yuz berdi.", parse_mode='Markdown')
        except TelegramError:
            pass
    finally:
        # Flag har qanday holatda tozalanadi — keyingi broadcast bloklanib qolmaydi
        if user_data is not None:
            user_data['broadcast_running'] = False


# Manba xabarga tegishli xatolar — HAR BIR manzil uchun qaytadi.
_SOURCE_ERROR_MARKERS = (
    'message to copy not found', 'message to forward not found',
    'message_id_invalid', 'message identifier is not specified',
    "message can't be copied", 'message is too long',
    'message text is empty', 'media_empty',
)

# Manzil haqiqatan yo'q / botni bloklagan — ro'yxatdan chiqarish o'rinli.
_DEAD_TARGET_MARKERS = (
    'chat not found', 'bot was kicked', 'bot is not a member',
    'user is deactivated', 'bot was blocked by the user',
    "can't initiate conversation", "can't talk to bots",
    'user not found', 'peer_id_invalid', 'chat_write_forbidden',
    'group chat was deactivated', 'the group chat was deleted',
)


def _classify_broadcast_error(err) -> str:
    """Broadcast xatosining turini aniqlaydi.

    'source'    — muammo YUBORILAYOTGAN xabarda (masalan admin uni o'chirib yuborgan).
                  Bu xato har bir manzil uchun qaytadi. Ilgari shu holatda BARCHA
                  guruhlar bazadan faolsizlantirilardi — bitta xato butun ro'yxatni
                  o'chirardi. Endi hech narsa o'chirilmaydi, tarqatish to'xtatiladi.
    'dead'      — manzil haqiqatan yo'q yoki botni bloklagan.
    'transient' — vaqtinchalik xato: hech narsa o'chirilmaydi.
    """
    msg = str(err).lower()
    if any(m in msg for m in _SOURCE_ERROR_MARKERS):
        return 'source'
    if any(m in msg for m in _DEAD_TARGET_MARKERS):
        return 'dead'
    if isinstance(err, Forbidden):
        return 'dead'
    return 'transient'


async def _report_broadcast_abort(status_message, reason: str, sent: int = 0, total: int = 0) -> None:
    text = (
        "🛑 **XABAR TARQATISH TO'XTATILDI**\n\n"
        "Yuborilayotgan xabarning o'zida muammo bor (o'chirilgan yoki nusxalab bo'lmaydi).\n\n"
        f"📤 To'xtashdan oldin yuborilgan: **{sent}/{total}**\n"
        "✅ **Hech bir guruh/foydalanuvchi ro'yxatdan chiqarilmadi.**\n\n"
        "Xabarni qayta yuborib ko'ring.\n\n"
        f"`{escape_markdown(reason[:200])}`"
    )
    try:
        await status_message.edit_text(text, parse_mode='Markdown')
    except TelegramError as e:
        logger.error(f"Abort hisobotini yuborib bo'lmadi: {e}")


async def _broadcast_send(bot, target_id, from_chat_id, message_id) -> None:
    """Bitta manzilga yuboradi. Telegram rate-limit so'rasa kutib bir marta qayta uriniladi."""
    try:
        await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)
    except RetryAfter as e:
        wait = min(float(getattr(e, 'retry_after', 5) or 5) + 1, 60)
        logger.warning(f"⏳ Rate limit: {wait:.0f}s kutib qayta urinamiz ({target_id})")
        await asyncio.sleep(wait)
        await bot.copy_message(chat_id=target_id, from_chat_id=from_chat_id, message_id=message_id)


async def _run_broadcast(bot, from_chat_id, message_id, broadcast_mode):
    groups = []
    users = []

    if broadcast_mode in ['groups', 'all']:
        groups = await db.get_active_groups()

    if broadcast_mode in ['users', 'all']:
        users = await db.get_all_users()

    total = len(groups) + len(users)

    if total == 0:
        await bot.send_message(chat_id=from_chat_id, text="❌ Xabar yuborish uchun manzillar topilmadi.", parse_mode='Markdown')
        return

    status_message = await bot.send_message(chat_id=from_chat_id, text=f"📤 Yuborilmoqda: 0/{total}", parse_mode='Markdown')

    success_count = 0
    fail_count = 0
    failed_list = []
    last_update_time = asyncio.get_event_loop().time()
    abort_reason = None

    for index, (chat_id, title) in enumerate(groups, 1):
        try:
            await _broadcast_send(bot, chat_id, from_chat_id, message_id)
            success_count += 1
            logger.info(f"Broadcast sent to group {title} ({chat_id})")
        except (Forbidden, BadRequest) as e:
            kind = _classify_broadcast_error(e)
            fail_count += 1
            failed_list.append(('group', title, str(e)))
            if kind == 'source':
                abort_reason = str(e)
                logger.error(f"🛑 Broadcast to'xtatildi — manba xabarda muammo: {e}")
                break
            if kind == 'dead':
                await db.deactivate_group(chat_id)
            logger.warning(f"Failed to send to group {title} ({kind}): {e}")
        except TelegramError as e:
            fail_count += 1
            failed_list.append(('group', title, str(e)))
            logger.error(f"Telegram error for group {title}: {e}")

        now = asyncio.get_event_loop().time()
        if now - last_update_time > 5:
            try:
                await status_message.edit_text(f"📤 Yuborilmoqda: {index}/{total}\n✅ Muvaffaqiyatli: {success_count}\n❌ Xatolik: {fail_count}", parse_mode='Markdown')
                last_update_time = now
            except TelegramError:
                pass

        await asyncio.sleep(config.BROADCAST_DELAY)

    if abort_reason:
        await _report_broadcast_abort(status_message, abort_reason, success_count, total)
        return

    for index, (user_id, username, first_name) in enumerate(users, len(groups) + 1):
        try:
            await _broadcast_send(bot, user_id, from_chat_id, message_id)
            success_count += 1
            # Yetdi — 'reachable' deb belgilaymiz (keyingi broadcastlarda tez topiladi)
            await db.set_reachable(user_id, True)
            logger.info(f"Broadcast sent to user {username or first_name} ({user_id})")
        except (Forbidden, BadRequest) as e:
            kind = _classify_broadcast_error(e)
            fail_count += 1
            failed_list.append(('user', username or first_name or str(user_id), str(e)))
            if kind == 'source':
                abort_reason = str(e)
                logger.error(f"🛑 Broadcast to'xtatildi — manba xabarda muammo: {e}")
                break
            if kind == 'dead':
                # Haqiqatan yetib bo'lmaydi — ro'yxatdan chiqaramiz.
                # Vaqtinchalik xatoda ESA chiqarmaymiz (ilgari chiqarilardi va
                # odam boshqa hech qachon xabar olmasdi).
                await db.set_reachable(user_id, False)
            logger.warning(f"Failed to send to user {user_id} ({kind}): {e}")
        except TelegramError as e:
            fail_count += 1
            failed_list.append(('user', username or first_name or str(user_id), str(e)))
            logger.error(f"Telegram error for user {user_id}: {e}")

        now = asyncio.get_event_loop().time()
        if now - last_update_time > 5:
            try:
                await status_message.edit_text(f"📤 Yuborilmoqda: {index}/{total}\n✅ Muvaffaqiyatli: {success_count}\n❌ Xatolik: {fail_count}", parse_mode='Markdown')
                last_update_time = now
            except TelegramError:
                pass

        await asyncio.sleep(config.BROADCAST_DELAY)

    if abort_reason:
        await _report_broadcast_abort(status_message, abort_reason, success_count, total)
        return

    await db.increment_stat('broadcasts_sent', success_count)

    result_text = f"✅ **XABAR YUBORILDI**\n\n📊 Jami: **{total}**\n✅ Muvaffaqiyatli: **{success_count}**\n❌ Xatolik: **{fail_count}**\n\n"

    if broadcast_mode == 'all':
        result_text += f"📢 Guruhlar: **{len(groups)}** ta\n👥 Foydalanuvchilar: **{len(users)}** ta\n\n"

    result_text += f"🕐 {format_uz_time()}"

    if failed_list:
        result_text += f"\n\n❌ **Xatolik bo'lganlar ({len(failed_list)} ta):**\n"

        for type_, name, error in failed_list[:15]:
            emoji = "📢" if type_ == 'group' else "👤"
            name_escaped = escape_markdown(name[:30])

            if 'Forbidden' in error:
                reason = "Bot bloklangan"
            elif 'not found' in error or 'chat not found' in error:
                reason = "Guruh topilmadi"
            elif 'deactivated' in error:
                reason = "User o'chirilgan"
            else:
                reason = "Xatolik"

            result_text += f"{emoji} {name_escaped} - {reason}\n"

        if len(failed_list) > 15:
            result_text += f"\n... va yana {len(failed_list) - 15} ta"

    await status_message.edit_text(result_text, parse_mode='Markdown')
    logger.info(f"Broadcast completed: {success_count}/{total} successful")


@error_handler
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PRODUCTION: Handle all group messages with smart admin detection.
    effective_message yangi VA tahrirlangan (edited) xabarlarni ham qamrab oladi —
    spammer toza xabar yuborib keyin link qo'shib tahrirlay olmaydi."""
    message = update.effective_message
    if not message or not message.from_user:
        return

    user = message.from_user
    chat = message.chat

    message_text = message.text or message.caption or "[media]"

    # Guruh ma'lumotini har xabarda emas, faqat keshda muddati o'tganda yangilaymiz
    # (har xabarda get_chat/get_chat_member_count chaqirish Telegram rate-limitini buzadi)
    await _maybe_update_group(context, chat)

    # KANAL/GURUH NOMIDAN yuborilgan xabarlar: oddiy admin tekshiruvi ularga ishlamaydi,
    # chunki from_user — soxta xizmat akkaunti. Shuning uchun alohida tekshiramiz.
    sender_verdict = await _check_sender_chat(context, message)
    if sender_verdict is not None:
        allowed, reason = sender_verdict
        if allowed:
            logger.info(
                f"✅ SENDER_CHAT BYPASS: chat={chat.id}, "
                f"sender_chat={message.sender_chat.id}, sabab={reason}"
            )
            await db.log_activity(message.sender_chat.id, message.sender_chat.username,
                                  message.sender_chat.title, chat.id, chat.title or "Unknown",
                                  message_text, "channel_message", f"allowed_via_{reason}")
            return
        await _handle_foreign_channel_message(context, message)
        return

    is_admin, verification_method = await is_definitely_admin(context, chat.id, user.id, user)

    if is_admin:
        logger.info(f"✅ ADMIN BYPASS: user_id={user.id}, username={user.username}, is_bot={user.is_bot}, chat={chat.id}, method={verification_method}")

        await db.update_user(user.id, user.username, user.first_name)
        await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", message_text, "admin_message", f"allowed_via_{verification_method}")

        return

    # FAIL-OPEN: agar admin tekshiruvi tarmoq/API xatosi tufayli AMALGA OSHMAGAN bo'lsa,
    # foydalanuvchini jazolamaymiz. Aks holda vaqtinchalik xatoda real admin reklamasi
    # o'chirilib, warning/ban olishi mumkin. Noaniqlikda admin foydasiga ishlaymiz.
    if verification_method == "verification_failed":
        logger.warning(
            f"⚠️ SKIP MODERATION: user {user.id} chat {chat.id} — admin holati aniqlanmadi "
            f"(API xatosi). Xavfsizlik uchun jazo qo'llanmadi."
        )
        await db.update_user(user.id, user.username, user.first_name)
        return

    logger.info(f"👤 REGULAR USER: user_id={user.id}, username={user.username}, is_bot={user.is_bot}, chat={chat.id}, verification={verification_method}")

    await db.update_user(user.id, user.username, user.first_name)

    if await flood_control.check(user.id, is_bot=user.is_bot, is_admin=is_admin, chat_id=chat.id):
        try:
            logger.warning(f"🚫 FLOOD DETECTED: user {user.id} in chat {chat.id}")

            await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", message_text, "flood", "blocked")
            await message.delete()

            user_mention = get_user_mention(user)
            warning_msg = await message.reply_text(f"⚠️ **{user_mention}**\n\n🚫 Flooding taqiqlanadi! Iltimos, sekinroq yozing.", parse_mode='Markdown')

            _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id, config.WARNING_DELETE_DELAY)
        except TelegramError as e:
            logger.error(f"Error handling flood: {e}")

        return

    text = message.text or message.caption or ""

    if contains_link(text):
        try:
            logger.warning(f"🚫 SPAM LINK DETECTED: user {user.id} in chat {chat.id}")

            try:
                await message.delete()
            except TelegramError as del_error:
                logger.warning(f"Could not delete spam message: {del_error}")

            await db.increment_stat('blocked_links')
            warn_count = await db.add_warning(user.id, user.username, user.first_name)
            await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", text, "spam_link", f"blocked_warn_{warn_count}")

            user_mention = get_user_mention(user)
            group_title = escape_markdown(chat.title or "guruhda")

            if warn_count <= 0:
                # Baza yozmadi — ogohlantirish soni noma'lum. Jazo (mute/ban) QO'LLANMAYDI.
                # Ilgari bu holatda foydalanuvchi "0/3 OGOHLANTIRISH" xabarini ko'rardi.
                logger.error(f"add_warning bazaga yozilmadi (user {user.id}) — jazo qo'llanmadi")
                warning_msg = await message.reply_text(f"⚠️ **{user_mention}**\n\n🚫 Reklama o'chirildi!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!", parse_mode='Markdown')

            elif warn_count >= config.MAX_WARNS:
                warning_msg = await message.reply_text(f"⛔ **{user_mention}**\n\n❌ **{warn_count}/{config.MAX_WARNS} OGOHLANTIRISH** — BAN!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!", parse_mode='Markdown')

                try:
                    await context.bot.ban_chat_member(chat.id, user.id)
                    await db.ban_user(user.id)
                    await db.increment_stat('banned_users')
                    logger.info(f"🔨 User {user.id} BANNED from {chat.id}")
                except TelegramError as e:
                    logger.error(f"Error banning user: {e}")

            elif warn_count == config.MAX_WARNS - 1:
                mute_until = get_uz_time() + timedelta(minutes=config.MUTE_DURATION_MINUTES)
                warning_msg = await message.reply_text(f"⚠️ **{user_mention}**\n\n🔇 **{warn_count}/{config.MAX_WARNS} OGOHLANTIRISH** — {config.MUTE_DURATION_MINUTES} daqiqa mute!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!", parse_mode='Markdown')

                try:
                    await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(can_send_messages=False), until_date=int(mute_until.timestamp()))
                    await db.increment_stat('muted_users')
                    logger.info(f"🔇 User {user.id} MUTED in {chat.id} for {config.MUTE_DURATION_MINUTES} min")
                except TelegramError as e:
                    logger.error(f"Error muting user: {e}")

            else:
                warning_msg = await message.reply_text(f"⚠️ **{user_mention}**\n\n🚫 **{warn_count}/{config.MAX_WARNS} OGOHLANTIRISH**\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!", parse_mode='Markdown')

            _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id, config.WARNING_DELETE_DELAY)

        except TelegramError as e:
            logger.error(f"Error handling link spam: {e}")

        return

    await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", message_text, "normal_message", "allowed")

    try:
        # Baza ulanishi Telegram API chaqiruvidan OLDIN yopiladi —
        # aks holda tarmoq kutilayotganda butun baza bloklanib turadi
        is_first_message = False
        async with db.get_connection() as conn:
            cursor = await conn.execute('SELECT message_count FROM users WHERE user_id = ?', (user.id,))
            row = await cursor.fetchone()
            is_first_message = bool(row and row['message_count'] == 1)

        if is_first_message:
            user_mention = get_user_mention(user)
            group_title = escape_markdown(chat.title or "guruh")

            welcome_msg = await message.reply_text(f"👋 Xush kelibsiz, **{user_mention}**!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!\n\n🛡 Bu guruh spam va reklamadan himoyalangan.", parse_mode='Markdown')
            _schedule_delete(context, welcome_msg.chat_id, welcome_msg.message_id, 15)

    except Exception as e:
        logger.error(f"Error sending welcome message: {e}")


@error_handler
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.message.chat
    group_title = escape_markdown(chat.title or "guruh")

    bot_info = await context.bot.get_me()
    bot_added = any(member.id == bot_info.id for member in update.message.new_chat_members)

    if bot_added:
        logger.info(f"🤖 Bot added to group {chat.id} ({group_title}) - refreshing admin list")
        await admin_manager.refresh_admins(context.bot, chat.id)

        try:
            bot_member = await context.bot.get_chat_member(chat.id, bot_info.id)

            if bot_member.status == 'administrator':
                can_delete = bot_member.can_delete_messages
                can_restrict = bot_member.can_restrict_members

                if can_delete and can_restrict:
                    setup_msg = await update.message.reply_text("✅ **Bot muvaffaqiyatli faollashtirildi!**\n\n🛡 Guruhingiz endi spam va reklamadan himoyalangan.\n\n**Bot qanday ishlaydi:**\n• Link va reklama avtomatik o'chiriladi\n• 3 marta ogohlantirish → ban\n• Adminlar erkn yoza oladi\n• Yangi a'zolarga xush kelibsiz xabari", parse_mode='Markdown')
                else:
                    setup_msg = await update.message.reply_text("⚠️ **Bot admin huquqlari to'liq emas!**\n\nGuruh to'liq ishlashi uchun botga quyidagi huquqlarni bering:\n\n✅ **Delete messages**\n✅ **Ban users**\n✅ **Restrict members**", parse_mode='Markdown')
            else:
                setup_msg = await update.message.reply_text("❌ **Bot admin emas!**\n\n⚠️ **MUHIM:** Guruh to'liq ishlashi uchun botni **ADMIN** qiling!", parse_mode='Markdown')

            _schedule_delete(context, setup_msg.chat_id, setup_msg.message_id, 60)

        except TelegramError as e:
            logger.error(f"Error sending setup instructions: {e}")

    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue

        user_mention = get_user_mention(new_member)

        try:
            welcome_msg = await update.message.reply_text(f"👋 Xush kelibsiz, **{user_mention}**!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!\n\n🛡 Bu guruh spam va reklamadan himoyalangan.", parse_mode='Markdown')
            _schedule_delete(context, welcome_msg.chat_id, welcome_msg.message_id, 15)
        except TelegramError as e:
            logger.error(f"Error sending welcome message: {e}")

        await db.update_user(new_member.id, new_member.username, new_member.first_name)

    try:
        await update.message.delete()
    except TelegramError:
        pass


async def cleanup_flood_data() -> None:
    await flood_control.cleanup()


async def post_init(application: Application) -> None:
    try:
        config.validate()
        await db.init()

        try:
            bot_info = await application.bot.get_me()
            admin_manager._bot_id = bot_info.id
            logger.info(f"✅ Bot ID cached: {bot_info.id}")
        except Exception as e:
            logger.error(f"Could not cache bot ID: {e}")

        groups = await db.get_active_groups()

        async def _initial_admin_refresh():
            """Admin ro'yxatlarini FONDA yuklaydi.

            Guruhlar orasida pauza bor (Telegram rate-limit). Fonda bajarilgani uchun
            100 ta guruhda ham bot darhol ishlashni boshlaydi, kutib turmaydi.
            """
            refreshed = 0
            for chat_id, title in groups:
                try:
                    if await admin_manager.refresh_admins(application.bot, chat_id):
                        refreshed += 1
                except Exception as e:
                    logger.warning(f"Could not refresh admins for {title}: {e}")
                await asyncio.sleep(config.ADMIN_REFRESH_DELAY)
            logger.info(f"✅ Admin lists refreshed for {refreshed}/{len(groups)} groups")

        logger.info(f"🔄 Admin ro'yxatlari fonda yuklanmoqda ({len(groups)} guruh)...")
        # Task'ga havola saqlanadi — aks holda garbage-collect bo'lib ketishi mumkin
        application.bot_data['initial_refresh_task'] = asyncio.create_task(_initial_admin_refresh())

        # Startup xabari: agar bot qayta-qayta qulab tushsa (Restart=always),
        # admin har 5 sekundda xabar olmasligi uchun 10 daqiqalik throttle.
        try:
            now_ts = int(datetime.now(tz=ZoneInfo('UTC')).timestamp())
            last_notify = await db.get_stat('last_startup_notify')
            if now_ts - last_notify > 600:
                await db.set_stat('last_startup_notify', now_ts)
                startup_msg = f"🚀 **BOT ISHGA TUSHDI**\n\n✅ Guruhlar: {len(groups)} ta\n✅ Adminlar fonda yuklanmoqda\n⏰ {format_uz_time()}\n\nBot muvaffaqiyatli ishga tushdi."
                await application.bot.send_message(chat_id=config.ADMIN_ID, text=startup_msg, parse_mode='Markdown')
                logger.info("✅ Startup notification sent")
            else:
                logger.warning("⚠️ Startup xabari yuborilmadi — bot yaqinda allaqachon ishga tushgan (restart loop?)")
        except Exception as e:
            logger.warning(f"Could not send startup notification: {e}")

        async def cleanup_activity_job():
            deleted = await db.cleanup_old_activity(days=7)
            if deleted:
                logger.info(f"🧹 Activity log: {deleted} ta eski yozuv o'chirildi")

        async def backup_job():
            await db.backup()

        async def refresh_all_admins():
            groups = await db.get_active_groups()
            active_ids = [chat_id for chat_id, _ in groups]
            refreshed = 0
            for chat_id in active_ids:
                # Keshi hali yangi bo'lsa API ni bezovta qilmaymiz
                if admin_manager.is_fresh(chat_id):
                    continue
                try:
                    if await admin_manager.refresh_admins(application.bot, chat_id):
                        refreshed += 1
                except Exception as e:
                    logger.warning(f"Admin yangilash xatosi (chat {chat_id}): {e}")
                # MUHIM: guruhlar orasida pauza. Ilgari pauza yo'q edi va 100 guruhda
                # har 10 daqiqada 100 ta so'rov birdan ketib, Telegram flood deb hisoblardi.
                await asyncio.sleep(config.ADMIN_REFRESH_DELAY)

            if refreshed:
                logger.info(f"🔄 Adminlar yangilandi: {refreshed} guruh")

            # Memory leak oldini olish: o'lik guruhlar keshini tozalaymiz
            admin_manager.prune(active_ids)
            active_set = set(active_ids)
            for cache in (_group_refresh, _linked_chat):
                for cid in list(cache.keys()):
                    if cid not in active_set:
                        del cache[cid]
            _prune_pm_caches()

        scheduler = AsyncIOScheduler(timezone=UZ_TZ)
        scheduler.add_job(cleanup_flood_data, 'interval', minutes=config.FLOOD_CLEANUP_MINUTES)
        scheduler.add_job(cleanup_activity_job, 'interval', hours=24)
        scheduler.add_job(refresh_all_admins, 'interval', minutes=10)
        # Har kuni 03:17 da bazaning nusxasi olinadi (oxirgi BACKUP_KEEP ta saqlanadi)
        scheduler.add_job(backup_job, 'cron', hour=3, minute=17)
        scheduler.start()

        # Ishga tushganda nusxa olamiz, lekin faqat oxirgisi 6 soatdan eski bo'lsa —
        # tez-tez restart kunlik nusxalarni o'chirib yubormasligi uchun
        await db.backup(only_if_older_than=6 * 3600)

        # Scheduler garbage-collect bo'lib ketmasligi uchun application'da saqlaymiz
        application.bot_data['scheduler'] = scheduler

        logger.info("✅ Scheduler started")
        logger.info("✅ Bot initialization completed")

    except Exception as e:
        logger.exception(f"❌ Error during initialization: {e}")
        sys.exit(1)


async def post_shutdown(application: Application) -> None:
    """Bot to'xtaganda scheduler va baza ulanishini tartibli yopadi."""
    scheduler = application.bot_data.get('scheduler')
    if scheduler is not None:
        try:
            scheduler.shutdown(wait=False)
            logger.info("✅ Scheduler to'xtatildi")
        except Exception as e:
            logger.warning(f"Scheduler to'xtatishda xato: {e}")
    await db.close()


async def error_callback(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    error_type = type(error).__name__
    error_msg = str(error)

    critical_errors = ['Unauthorized', 'InvalidToken']
    is_critical = any(err in error_type for err in critical_errors)

    # Conflict = bir token ostida IKKI bot instansi ishlamoqda (deploy muammosi).
    # Buni jim yutmaymiz — aniq ERROR sifatida log qilamiz, aks holda muammo berkitiladi.
    if 'Conflict' in error_type:
        logger.error(f"🚨 CONFLICT: bir token ostida bir nechta bot instansi ishlayotgan bo'lishi mumkin! {error_msg}")
        return

    # Oddiy tarmoq/ulanish xatolari — adminni spamlamaymiz
    ignored_errors = ['NetworkError', 'TimedOut', 'ReadError']
    if any(err in error_type for err in ignored_errors):
        logger.warning(f"⚠️ Oddiy tarmoq/ulanish xatosi: {error_type} - {error_msg}")
        return

    if is_critical:
        # InvalidToken/Unauthorized — bot ishlay olmaydi, AVTOMATIK tiklanmaydi. Aniq ERROR.
        logger.error(f"🚨 KRITIK: {error_type} - {error_msg} — bot token/ruxsatini tekshiring!")

        # Throttle: bir xil kritik xato takrorlanganda admin chati spamlanmasligi uchun
        global _critical_alert_ts
        now = asyncio.get_event_loop().time()
        if now - _critical_alert_ts < _CRITICAL_ALERT_INTERVAL:
            return
        _critical_alert_ts = now

        try:
            alert_text = (
                f"🚨 **KRITIK BOT XATOLIGI**\n\n"
                f"⚠️ Turi: `{error_type}`\n📝 Xabar: {error_msg[:200]}\n⏰ {format_uz_time()}\n\n"
                f"❗️ Bot ishlay olmayapti — token yoki ruxsatlarni darhol tekshiring."
            )
            await context.bot.send_message(chat_id=config.ADMIN_ID, text=alert_text, parse_mode='Markdown')
            logger.info(f"✅ Critical error alert sent: {error_type}")
        except Exception as e:
            logger.error(f"Failed to send error alert: {e}")
    else:
        logger.error(f"Exception while handling update {update}:", exc_info=error)


def main() -> None:
    logger.info("=" * 60)
    logger.info("🚀 Starting Professional Anti-Spam Bot - PRODUCTION")
    logger.info("=" * 60)

    try:
        config.validate()
    except ValueError as e:
        logger.error(f"❌ Configuration error: {e}")
        sys.exit(1)

    logger.info(f"✅ Admin ID: {config.ADMIN_ID} (jami adminlar: {len(config.ADMIN_IDS)})")
    logger.info(f"✅ Bot Username: {config.BOT_USERNAME}")
    logger.info(f"✅ Database: {config.DB_FILE}")
    logger.info(f"✅ Backup: {config.BACKUP_DIR} (oxirgi {config.BACKUP_KEEP} ta)")
    logger.info(f"✅ @mention bloklash: {'HA' if config.BLOCK_MENTIONS else 'YO\'Q (faqat reklama konteksti)'}")
    logger.info(f"✅ Xabar matnini log qilish: {'HA' if config.LOG_MESSAGE_TEXT else 'YO\'Q (maxfiylik)'}")
    logger.info("🔍 Bot will auto-clear conflicts on startup...")

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_error_handler(error_callback)

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("activity", activity_handler))
    application.add_handler(CommandHandler("users", users_handler))
    application.add_handler(CommandHandler("refreshadmins", refresh_admins_handler))
    application.add_handler(CommandHandler("cancel", cancel_handler))
    application.add_handler(CommandHandler("unwarn", unwarn_handler))
    application.add_handler(CommandHandler("unban", unban_handler))
    application.add_handler(CommandHandler("unmute", unmute_handler))

    application.add_handler(CallbackQueryHandler(admin_panel_handler, pattern="^admin$"))
    application.add_handler(CallbackQueryHandler(stats_handler, pattern="^stats$"))
    application.add_handler(CallbackQueryHandler(groups_list_handler, pattern="^groups_list$"))
    application.add_handler(CallbackQueryHandler(users_count_handler, pattern="^users_count$"))
    application.add_handler(CallbackQueryHandler(broadcast_menu_handler, pattern="^broadcast_menu$"))
    application.add_handler(CallbackQueryHandler(broadcast_groups_handler, pattern="^broadcast_groups$"))
    application.add_handler(CallbackQueryHandler(broadcast_users_handler, pattern="^broadcast_users$"))
    application.add_handler(CallbackQueryHandler(broadcast_all_handler, pattern="^broadcast_all$"))
    application.add_handler(CallbackQueryHandler(help_handler, pattern="^help$"))
    application.add_handler(CallbackQueryHandler(back_handler, pattern="^back$"))

    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler))
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, private_message_handler))
    # Guruh xabarlari. MessageHandler default holatda tahrirlangan (edited) xabarlarni ham
    # ushlaydi, group_message_handler esa effective_message ishlatadi — shuning uchun
    # link bypass (toza yuborib keyin tahrirlash) avtomatik qamrab olinadi.
    application.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.COMMAND, group_message_handler))

    logger.info("✅ All handlers registered")
    logger.info("🟢 Bot is running...")
    logger.info("=" * 60)

    if config.WEBHOOK_URL:
        logger.info(f"🌐 Starting Custom Synchronous Webhook on port {config.PORT}")
        
        async def custom_webhook(request: web.Request) -> web.Response:
            try:
                # XAVFSIZLIK: Telegram har so'rovda shu sarlavhani yuboradi.
                # Tekshirmasak — URL'ni bilgan har kim soxta "xabar" yuborib,
                # bot nomidan buyruq bajartirishi mumkin.
                if config.WEBHOOK_SECRET:
                    sent = request.headers.get('X-Telegram-Bot-Api-Secret-Token')
                    if sent != config.WEBHOOK_SECRET:
                        logger.warning("🚫 Webhook: noto'g'ri secret token — so'rov rad etildi")
                        return web.Response(status=403, text="forbidden")

                # 1. Parse update
                data = await request.json()
                update = Update.de_json(data, application.bot)
                
                # 2. SYNCHRONOUSLY process update before returning 200 OK!
                # This keeps Cloud Run CPU ALIVE until the bot finishes replying!
                await application.process_update(update)
                
                return web.Response(status=200, text="OK")
            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return web.Response(status=200, text="OK")

        async def health_check(request: web.Request) -> web.Response:
            return web.Response(status=200, text="Bot is running")

        app = web.Application()
        app.router.add_post("/", custom_webhook)
        app.router.add_get("/", health_check)

        # We must manually set the webhook and init the application
        async def start_server():
            await application.initialize()
            await application.start()
            await application.bot.set_webhook(
                url=config.WEBHOOK_URL,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=config.DROP_PENDING_UPDATES,
                secret_token=config.WEBHOOK_SECRET or None,
            )

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", config.PORT)
            await site.start()
            logger.info("✅ Custom Webhook server started successfully!")

            try:
                # Keep alive
                while True:
                    await asyncio.sleep(3600)
            finally:
                # Toza yopilish — har qanday holatda resurslar bo'shatiladi
                await runner.cleanup()
                await application.stop()
                await application.shutdown()

        # asyncio.run zamonaviy va deprecated get_event_loop() o'rnini bosadi
        try:
            asyncio.run(start_server())
        except KeyboardInterrupt:
            pass

    else:
        logger.info("🔄 Starting long-polling...")
        # drop_pending_updates=False (default): bot o'chganda kelgan xabarlar ham
        # ko'rib chiqiladi. Ilgari True edi — o'chiq paytdagi spam e'tiborsiz qolardi.
        application.run_polling(allowed_updates=Update.ALL_TYPES,
                                drop_pending_updates=config.DROP_PENDING_UPDATES)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")
        sys.exit(1)