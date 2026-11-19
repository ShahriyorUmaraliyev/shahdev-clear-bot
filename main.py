import logging
import re
import os
import sys
import time
import asyncio

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
    ApplicationHandlerStop,
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


# Faqat haqiqiy butun son: "-100123" ha, "--5" yoki "12a" yo'q
_INT_RE = re.compile(r'-?\d+')


def _env_bool(name: str, default: bool = False) -> bool:
    """ENV dan bool o'qish: 1/true/yes/on/ha -> True."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on', 'ha')


def _env_ids(name: str) -> set:
    """ENV dan vergul/bo'shliq bilan ajratilgan ID ro'yxatini o'qish.

    Faqat to'g'ri butun son qabul qilinadi. Ilgari "--5" kabi qiymat
    `lstrip('-').isdigit()` tekshiruvidan o'tib, int() da bot ishga tushmay qolardi.
    """
    out = set()
    for part in re.split(r'[,\s;]+', os.getenv(name, "") or ""):
        part = part.strip()
        if _INT_RE.fullmatch(part):
            out.add(int(part))
        elif part:
            logging.getLogger(__name__).warning(
                "%s ichidagi noto'g'ri qiymat e'tiborsiz qoldirildi: %r", name, part)
    return out


@dataclass
class Config:
    """Centralized bot configuration"""
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0") or "0")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")

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
    # Ogohlantirish shu kundan keyin o'z-o'zidan kuyib ketadi (har guruh uchun alohida)
    WARN_TTL_DAYS: int = 30
    MUTE_DURATION_MINUTES: int = 10
    FLOOD_LIMIT: int = 3
    FLOOD_WINDOW_SECONDS: int = 5
    WARNING_DELETE_DELAY: int = 10
    BROADCAST_DELAY: float = 0.3
    FLOOD_CLEANUP_MINUTES: int = 5

    DB_TIMEOUT: int = 10

    PM_FLOOD_LIMIT: int = 5
    # Bitta odamdan kuniga shuncha xabar adminga yo'naltiriladi, keyin to'xtaydi
    PM_DAILY_LIMIT: int = 30
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
            if _INT_RE.fullmatch(part):
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
            # domen: SUBDOMENLAR BILAN. Ilgari "(?<!...)" nuqtadan keyingi qismni
            # tutmasdi va shop.spam.com, my.casino.com kabi havolalar o'tib ketardi.
            # Oxirgi bo'g'inda kamida bitta HARF bo'lishi shart — shunda "versiya 1.0.net"
            # yoki IP-ga o'xshash raqamlar domen deb hisoblanmaydi.
            r'(?<![a-zA-Z0-9@.-])(?:[a-zA-Z0-9][a-zA-Z0-9-]*\.)*[a-zA-Z0-9-]*[a-zA-Z][a-zA-Z0-9-]*\.(com|net|org|uz|ru|me|io|co|info|online|site|kg|tj|xyz|top|shop|store|link|club)\b',
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
        except ApplicationHandlerStop:
            # Bu xato emas — "boshqa handlerlar ishlamasin" degan signal
            raise
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
        # Faqat ADMIN ozod. Ilgari `is_bot` ham ozod edi va mantiq qarama-qarshi chiqardi:
        # begona bot flood qilsa hech narsa bo'lmasdi, lekin link yuborsa ban bo'lardi.
        if is_admin:
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


async def get_bot_id(context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    """Bot ID keshdan olinadi.

    Ilgari har yangi a'zo qo'shilganda get_me() chaqirilardi — bu keraksiz
    Telegram so'rovi, chunki ID bir marta aniqlansa o'zgarmaydi.
    """
    if admin_manager._bot_id is None:
        try:
            admin_manager._bot_id = (await context.bot.get_me()).id
        except TelegramError as e:
            logger.error(f"Bot ID aniqlanmadi: {e}")
            return None
    return admin_manager._bot_id


async def is_definitely_admin(context: ContextTypes.DEFAULT_TYPE,
                              chat_id: int,
                              user_id: int,
                              user) -> Tuple[bool, str]:
    """Universal admin verification - PRODUCTION READY"""

    # LAYER 0: Anonymous Admin Messages (Telegram official).
    # Username ba'zan bo'sh kelishi mumkin — shuning uchun ID bo'yicha ham tekshiramiz.
    if user.is_bot and (user.username == "GroupAnonymousBot" or user.id == GROUP_ANONYMOUS_BOT_ID):
        logger.info("✅ LAYER 0: Anonymous admin message detected")
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
                    # DIQQAT: _last_refresh YANGILANMAYDI. Ilgari yangilanardi va
                    # kesh "to'liq va yangi" deb hisoblanib, haqiqiy to'liq yangilanish
                    # 10 daqiqa kechikardi (ro'yxatda esa faqat bitta odam bo'lardi).
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

                    # Ogohlantirishlar HAR GURUH uchun alohida saqlanadi.
                    # Ilgari users.warn_count umumiy edi: 3 xil guruhda bittadan
                    # ogohlantirish olgan odam ban bo'lardi.
                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS chat_warnings (
                            chat_id INTEGER NOT NULL,
                            user_id INTEGER NOT NULL,
                            count INTEGER NOT NULL DEFAULT 0,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (chat_id, user_id)
                        )
                    ''')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_warn_updated ON chat_warnings(updated_at)')

                    stats_keys = ['total_messages', 'blocked_links', 'banned_users', 'muted_users', 'total_warnings', 'broadcasts_sent']
                    for key in stats_keys:
                        await conn.execute('INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)', (key,))

                    await conn.commit()
            except Exception as e:
                logger.exception(f"Database initialization failed: {e}")
                raise

    async def update_user(self, user_id: int, username: str, first_name: str,
                          count_message: bool = True, bump_total: bool = False) -> None:
        """Foydalanuvchini bazaga yozadi.

        count_message=False — faqat ism/username yangilanadi (guruhga kirish kabi
        hodisalarda xabar soni bekorga ko'paymasligi uchun).
        bump_total=True — faqat GURUH xabarlari umumiy hisobga qo'shiladi
        (ilgari shaxsiy xabarlar va guruhga kirishlar ham sanalardi).
        """
        try:
            inc = 1 if count_message else 0
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, message_count, warn_count)
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(user_id) DO UPDATE SET
                        username = excluded.username,
                        first_name = excluded.first_name,
                        message_count = users.message_count + ?,
                        updated_at = CURRENT_TIMESTAMP
                ''', (user_id, username, first_name, inc, inc))
                if bump_total:
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

    async def add_warning(self, chat_id: int, user_id: int, username: str, first_name: str) -> int:
        """Shu GURUHDAGI ogohlantirishlar sonini bittaga oshiradi va yangisini qaytaradi.

        Oxirgi ogohlantirish WARN_TTL_DAYS kundan eski bo'lsa — hisob 1 dan boshlanadi
        (eski gunoh abadiy yopishib qolmaydi). Xato bo'lsa 0 qaytaradi; chaqiruvchi
        0 da JAZO QO'LLAMAYDI.
        """
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('''
                    INSERT INTO chat_warnings (chat_id, user_id, count)
                    VALUES (?, ?, 1)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET
                        count = CASE
                            WHEN chat_warnings.updated_at < datetime('now', ?) THEN 1
                            ELSE chat_warnings.count + 1
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING count
                ''', (chat_id, user_id, f'-{config.WARN_TTL_DAYS} days'))
                row = await cursor.fetchone()
                warn_count = row['count'] if row else 0

                # users.warn_count — umrbod umumiy hisob (faqat ko'rsatish uchun)
                await conn.execute('''
                    INSERT INTO users (user_id, username, first_name, message_count, warn_count)
                    VALUES (?, ?, ?, 0, 1)
                    ON CONFLICT(user_id) DO UPDATE SET
                        warn_count = users.warn_count + 1,
                        username = excluded.username,
                        first_name = excluded.first_name,
                        updated_at = CURRENT_TIMESTAMP
                ''', (user_id, username, first_name))
                await conn.execute('UPDATE stats SET value = value + 1, updated_at = CURRENT_TIMESTAMP WHERE key = ?', ('total_warnings',))
                await conn.commit()
                return warn_count
        except Exception as e:
            logger.error(f"Error adding warning for user {user_id} in chat {chat_id}: {e}")
            return 0

    async def get_warnings(self, chat_id: int, user_id: int) -> int:
        """Shu guruhdagi joriy ogohlantirishlar soni (muddati o'tganlari 0)."""
        try:
            async with self.get_connection() as conn:
                row = await (await conn.execute(
                    "SELECT count FROM chat_warnings WHERE chat_id = ? AND user_id = ? "
                    "AND updated_at >= datetime('now', ?)",
                    (chat_id, user_id, f'-{config.WARN_TTL_DAYS} days'))).fetchone()
                return row['count'] if row else 0
        except Exception as e:
            logger.error(f"get_warnings xatosi: {e}")
            return 0

    async def cleanup_old_warnings(self) -> int:
        """Muddati o'tgan ogohlantirish yozuvlarini o'chiradi."""
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute(
                    "DELETE FROM chat_warnings WHERE updated_at < datetime('now', ?)",
                    (f'-{config.WARN_TTL_DAYS} days',))
                deleted = cursor.rowcount
                await conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"cleanup_old_warnings xatosi: {e}")
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

    async def reset_warnings(self, user_id: int, chat_id: Optional[int] = None) -> None:
        """Ogohlantirishlarni tozalaydi. chat_id berilsa faqat o'sha guruhda."""
        try:
            async with self.get_connection() as conn:
                if chat_id is None:
                    await conn.execute('DELETE FROM chat_warnings WHERE user_id = ?', (user_id,))
                else:
                    await conn.execute('DELETE FROM chat_warnings WHERE chat_id = ? AND user_id = ?',
                                       (chat_id, user_id))
                await conn.execute('UPDATE users SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception as e:
            logger.error(f"reset_warnings({user_id}) xatosi: {e}")

    async def get_stats(self):
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('SELECT COUNT(*) as count FROM users')
                users_count = (await cursor.fetchone())['count']

                # Obunachi = botni shaxsiy chatda ochgan (broadcast yetib boradigan).
                # Ilgari panelda faqat "jami" ko'rsatilardi va u chalg'ituvchi edi.
                cursor = await conn.execute('SELECT COUNT(*) as count FROM users WHERE started_bot = 1')
                subs_count = (await cursor.fetchone())['count']

                cursor = await conn.execute('SELECT COUNT(*) as count FROM groups WHERE is_active = 1')
                groups_count = (await cursor.fetchone())['count']

                cursor = await conn.execute('SELECT key, value FROM stats')
                stats_rows = await cursor.fetchall()
                stats = {row['key']: row['value'] for row in stats_rows}

                return {'users': users_count, 'subscribers': subs_count,
                        'groups': groups_count, **stats}
        except Exception as e:
            logger.error(f"get_stats xatosi: {e}")
            return {'users': 0, 'subscribers': 0, 'groups': 0, 'blocked_links': 0, 'banned_users': 0, 'total_messages': 0, 'muted_users': 0, 'total_warnings': 0, 'broadcasts_sent': 0}

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
    """Markdown-xavfsiz mention. Username bo'lsa @username, aks holda bosiladigan havola.

    DIQQAT: ism safe_md() bilan tozalanadi — aks holda "Ali](http://evil.com)" kabi ism
    botning o'z ogohlantirishini spam havolaga aylantirib yuboradi.
    """
    if user is None:
        return "Foydalanuvchi"
    if getattr(user, 'username', None):
        return f"@{safe_md(user.username)}"
    first_name = getattr(user, 'first_name', None) or "Foydalanuvchi"
    return f"[{safe_md(first_name)}](tg://user?id={user.id})"


# Markdown v1 "]" , "(" , ")" belgilarini QALQONLAY OLMAYDI.
# Shu sababli ismi "Ali](http://evil.com)" bo'lgan odam botning o'z xabari ichiga
# bosiladigan havola joylashi mumkin edi. Ularni butunlay olib tashlaymiz.
_MD_STRIP = str.maketrans({']': '', '(': '', ')': ''})


def safe_md(text) -> str:
    """Foydalanuvchi/guruh nomini Markdown ichiga qo'yishdan oldin tozalaydi."""
    return escape_markdown(str(text if text is not None else '').translate(_MD_STRIP))


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


def _hidden_link_entity(message) -> Optional[str]:
    """Matn ostiga YASHIRILGAN havolani topadi (text_link).

    Telegram'da "Bu yerni bosing" so'zining ostiga havola yashirish mumkin —
    matnning o'zida hech qanday link ko'rinmaydi. Ilgari bot entity'larni
    umuman o'qimasdi va bunday reklama bemalol o'tib ketardi.
    """
    for ents in (getattr(message, 'entities', None), getattr(message, 'caption_entities', None)):
        for ent in (ents or ()):
            if getattr(ent, 'type', None) == 'text_link' and getattr(ent, 'url', None):
                return ent.url
    return None


def message_has_link(message) -> Tuple[bool, str]:
    """Xabarda reklama/havola bormi. (bor_yoq, sabab) qaytaradi."""
    text = (getattr(message, 'text', None) or getattr(message, 'caption', None) or "")
    if contains_link(text):
        return True, 'matn'
    hidden = _hidden_link_entity(message)
    if hidden:
        return True, f'yashirin_havola:{hidden[:80]}'
    return False, ''


_PLAIN = str.maketrans({'*': '', '_': '', '`': '', '[': '', ']': '', '\\': ''})


def plain_text(text: str) -> str:
    """Markdown belgilarini olib tashlaydi (zaxira variant uchun)."""
    return text.translate(_PLAIN)


async def safe_reply(message, text: str):
    """Markdown bilan javob beradi; parse xatosida oddiy matnda qayta uriniladi.

    MUHIM: ilgari bitta buzuq belgi (masalan ism ichidagi "]") butun bloka xato
    berib, undan keyingi JAZO (ban/mute) bajarilmay qolardi.
    """
    try:
        return await message.reply_text(text, parse_mode='Markdown')
    except BadRequest as e:
        logger.warning(f"Markdown buzildi, oddiy matnda yuborilmoqda: {e}")
        try:
            return await message.reply_text(plain_text(text))
        except TelegramError as e2:
            logger.error(f"Javob yuborilmadi: {e2}")
            return None
    except TelegramError as e:
        logger.error(f"Javob yuborilmadi: {e}")
        return None


async def safe_send(bot, chat_id: int, text: str):
    """safe_reply ning send_message varianti."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode='Markdown')
    except BadRequest as e:
        logger.warning(f"Markdown buzildi, oddiy matnda yuborilmoqda: {e}")
        try:
            return await bot.send_message(chat_id=chat_id, text=plain_text(text))
        except TelegramError as e2:
            logger.error(f"Xabar yuborilmadi: {e2}")
            return None
    except TelegramError as e:
        logger.error(f"Xabar yuborilmadi: {e}")
        return None


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


# Guruh ma'lumotini yangilash keshi: {chat_id: (last_fetch_ts, oxirgi_yozilgan_nom)}
_group_refresh: Dict[int, Tuple[float, Optional[str]]] = {}
_GROUP_REFRESH_INTERVAL = 3600  # 1 soat

# Adminga "yangi xabar" bildirishnomasi throttle keshi: {user_id: last_notify_ts}
_pm_notify_ts: Dict[int, float] = {}
_PM_NOTIFY_INTERVAL = 300  # har userdan info-header eng ko'pi 5 daqiqada bir marta

# Shaxsiy chatda flood ogohlantirishi throttle keshi: {user_id: last_warn_ts}
_pm_flood_warn_ts: Dict[int, float] = {}
_PM_FLOOD_WARN_INTERVAL = 60

# Kunlik chek: {user_id: (sana, soni}. Burst limiti 10 sekundda 5 ta edi —
# ya'ni bitta odam kuniga 40 mingdan ortiq xabarni adminning chatiga
# yo'naltirishi mumkin edi. Endi kunlik chek ham bor.
_pm_daily: Dict[int, Tuple[str, int]] = {}

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

    # Kunlik hisob: bugungi bo'lmagan yozuvlar keraksiz
    today = get_uz_time().strftime('%Y-%m-%d')
    for uid, (day, _cnt) in list(_pm_daily.items()):
        if day != today:
            del _pm_daily[uid]
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


async def _handle_foreign_channel_message(context: ContextTypes.DEFAULT_TYPE, message) -> bool:
    """Begona kanal nomidan yozilgan xabar. O'chirilgan bo'lsa True qaytaradi.

    Kanalga odam kabi ogohlantirish berib bo'lmaydi, lekin Telegram
    ban_chat_sender_chat ni beradi — shuning uchun takroriy reklamadan keyin
    kanalning o'zi guruhdan bloklanadi. Ilgari xabar faqat o'chirilardi va
    kanali bor spammer cheksiz yozaverardi.
    """
    chat = message.chat
    sender_chat = message.sender_chat
    text = message.text or message.caption or ""

    has_link, link_reason = message_has_link(message)
    if not has_link:
        return False

    logger.warning(
        f"🚫 KANAL REKLAMASI: sender_chat={sender_chat.id} "
        f"(@{sender_chat.username or '—'}) chat={chat.id} sabab={link_reason}"
    )

    try:
        await message.delete()
    except TelegramError as e:
        logger.warning(f"Kanal xabarini o'chirib bo'lmadi: {e}")
        return False

    await db.increment_stat('blocked_links')
    warn_count = await db.add_warning(chat.id, sender_chat.id,
                                      sender_chat.username, sender_chat.title)
    await db.log_activity(sender_chat.id, sender_chat.username, sender_chat.title,
                          chat.id, chat.title or "Unknown", text, "spam_link",
                          f"blocked_channel_warn_{warn_count}")

    extra = ""
    if 0 < warn_count and warn_count >= config.MAX_WARNS:
        try:
            await context.bot.ban_chat_sender_chat(chat.id, sender_chat.id)
            extra = "\n\n⛔️ Takroriy reklama uchun kanal guruhdan bloklandi."
            logger.info(f"⛔️ Kanal {sender_chat.id} guruh {chat.id} dan bloklandi")
        except TelegramError as e:
            logger.error(f"Kanalni bloklab bo'lmadi: {e}")

    name = safe_md(sender_chat.title or sender_chat.username or "Kanal")
    warning_msg = await safe_send(
        context.bot, chat.id,
        f"🚫 **{name}** kanali nomidan yuborilgan reklama o'chirildi.{extra}")
    if warning_msg is not None:
        _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id,
                         config.WARNING_DELETE_DELAY)
    return True


async def _maybe_update_group(context: ContextTypes.DEFAULT_TYPE, chat) -> None:
    """Guruhni DB ga yozadi — lekin HAR XABARDA emas.

    Ilgari member_count uchun API chaqiruvi throttle qilingan, bazaga yozish esa
    har bir xabarda bajarilardi (sekundiga yuzlab keraksiz yozuv). Endi yozuv
    faqat nom o'zgarganda yoki throttle muddati tugaganda bajariladi.
    """
    now = asyncio.get_event_loop().time()
    last, known_title = _group_refresh.get(chat.id, (0.0, None))
    title = chat.title or "Unknown"

    if now - last > _GROUP_REFRESH_INTERVAL:
        try:
            member_count = await context.bot.get_chat_member_count(chat.id)
            await db.update_group(chat.id, title, member_count)
            _group_refresh[chat.id] = (now, title)
            return
        except TelegramError as e:
            # Xato bo'lsa bir soat kutmaymiz — 5 daqiqadan keyin qayta urinamiz
            _group_refresh[chat.id] = (now - _GROUP_REFRESH_INTERVAL + 300, known_title)
            logger.debug(f"get_chat_member_count({chat.id}) xatosi: {e}")

    if known_title != title:
        # Nom o'zgardi — faqat shu holatda yozamiz (API chaqiruvisiz)
        await db.update_group(chat.id, title)
        _group_refresh[chat.id] = (_group_refresh.get(chat.id, (now, None))[0], title)


# Guruhda /start ga javob berish throttle'i: {chat_id: last_reply_ts}
_group_start_reply: Dict[int, float] = {}
_GROUP_START_REPLY_INTERVAL = 300


@error_handler
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.from_user:
        return

    user = message.from_user

    if message.chat.type != 'private':
        # GURUHDA: javob 5 daqiqada eng ko'pi bir marta va o'zi o'chadi.
        # Ilgari har /start ga javob yozilardi — istalgan odam bot bilan
        # guruhni spamlay olardi (komandalar flood hisobiga ham tushmaydi).
        now = asyncio.get_event_loop().time()
        if now - _group_start_reply.get(message.chat.id, 0) < _GROUP_START_REPLY_INTERVAL:
            return
        _group_start_reply[message.chat.id] = now
        reply = await safe_reply(
            message, "✅ Bot faol ishlayapti!\n🛡 Guruh spam va reklamadan himoyalangan.")
        if reply is not None:
            _schedule_delete(context, reply.chat_id, reply.message_id, 15)
        return

    is_new_user = False
    try:
        async with db.get_connection() as conn:
            existing_user = await (await conn.execute('SELECT user_id FROM users WHERE user_id = ?', (user.id,))).fetchone()
            is_new_user = existing_user is None
    except Exception as e:
        logger.error(f"Error checking if user is new: {e}")
        is_new_user = False

    await db.update_user(user.id, user.username, user.first_name, count_message=False)
    # Shaxsiy chatda /start bosgan — broadcast yetib boradigan user deb belgilaymiz
    await db.mark_user_started(user.id, user.username, user.first_name)

    if is_new_user:
        celebration_text = (
            f"🎉 **TABRIKLAYMIZ, {safe_md(user.first_name)}!**\n\n"
            f"🎊 Siz botimizning yangi foydalanuvchisisiz!\n\n"
            f"✨ Botdan foydalanganingiz uchun tashakkur!\n"
            f"🛡 Endi guruhlaringizni himoya qilishingiz mumkin."
        )
        # DIQQAT: bu yerda asyncio.sleep() YO'Q — u butun botni to'xtatib turardi.
        await safe_reply(message, celebration_text)

        username_display = f"@{safe_md(user.username)}" if user.username else safe_md(user.first_name)
        admin_notification = (
            f"🎉 **YANGI FOYDALANUVCHI!**\n\n"
            f"👤 Ism: {safe_md(user.first_name)}\n"
            f"🆔 Username: {username_display}\n"
            f"🔢 ID: `{user.id}`\n"
            f"⏰ {format_uz_time()}\n\n"
            f"🎊 Bot foydalanuvchilari yana 1 taga ko'paydi!"
        )
        if await safe_send(context.bot, config.ADMIN_ID, admin_notification) is not None:
            logger.info(f"✅ New user notification: {user.id}")

    welcome_text = (
        f"👋 Assalomu alaykum, **{safe_md(user.first_name)}**!\n\n"
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
        await message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except BadRequest:
        await message.reply_text(plain_text(welcome_text), reply_markup=InlineKeyboardMarkup(keyboard))


@error_handler
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
        f"   • Bot ko'rgan: **{stats['users']:,}** ta\n"
        f"   • Obunachi (xabar yetadi): **{stats.get('subscribers', 0):,}** ta\n"
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
        # Guruh nomi qalqonlanadi: nomida "_" yoki "[" bo'lgan guruh
        # ilgari butun panelni ishdan chiqarardi (BadRequest: can't parse entities)
        groups_text = "\n".join([f"{i}. {safe_md(title)}" for i, (_, title) in enumerate(groups[:25], 1)])
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
                username_escaped = safe_md(username)
                msg_count = user['message_count']
                banned = "🚫" if user['is_banned'] else ""

                text += f"{i}. {username_escaped} {banned}\n   💬 {msg_count} xabar\n"

            if total_users > 10:
                text += f"\n... va yana {total_users - 10:,} ta\n"

            text += "\n💡 To'liq ro'yxat: /users"

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
        f"👋 Assalomu alaykum, **{safe_md(user.first_name)}**!\n\n"
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
        await safe_reply(update.message, f"📊 **ACTIVITY LOG**\n\nOxirgi {hours} soatda faoliyat yo'q.")
        return

    text = f"📊 **ACTIVITY LOG** (Oxirgi {hours} soat)\n\n"

    for i, act in enumerate(activities[:20], 1):
        # DIQQAT: bu yerdagi matnlar SPAMMERNING O'ZI yozgan matn.
        # Ilgari qalqonlanmasdi va spammer "[bosing](http://evil.com)" yozsa,
        # bot o'sha havolani adminning chatida bosiladigan qilib ko'rsatardi
        # (yoki butun /activity "can't parse entities" bilan ishdan chiqardi).
        username = safe_md(act['username'] or act['first_name'] or "Unknown")
        action = safe_md(act['action_taken'])
        chat_title = safe_md(act['chat_title'])
        raw_text = act['message_text'] or "—"
        msg_preview = safe_md((raw_text[:30] + "...") if len(raw_text) > 30 else raw_text)
        try:
            ts = act['timestamp']
            dt_utc = ts if isinstance(ts, datetime) else datetime.strptime(str(ts).split('.')[0], '%Y-%m-%d %H:%M:%S')
            if dt_utc.tzinfo is None:
                dt_utc = dt_utc.replace(tzinfo=ZoneInfo('UTC'))
            timestamp = dt_utc.astimezone(UZ_TZ).strftime('%d.%m.%Y %H:%M:%S')
        except Exception:
            timestamp = safe_md(str(act['timestamp']).split('.')[0])

        text += f"{i}. **{username}** | {chat_title}\n   💬 {msg_preview}\n   ⚡ {action} | {timestamp}\n\n"

    if len(activities) > 20:
        text += f"... va yana {len(activities) - 20} ta faoliyat\n\n"

    text += f"📍 Jami: **{len(activities)}** ta faoliyat\n⏰ Yangilangan: {format_uz_time(format_str='%H:%M:%S')}"

    await safe_reply(update.message, text)


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

            username_escaped = safe_md(username)
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

    groups = await db.get_active_groups()
    status_msg = await safe_reply(
        update.message, f"🔄 Admin ro'yxatlari fonda yangilanmoqda ({len(groups)} guruh)...")

    async def _job():
        """Fonda ishlaydi: ilgari bu sikl butun botni bloklab turardi
        (100 guruh x pauza), pauzasiz esa Telegram flood-limitiga tushardi."""
        success = 0
        failed = 0
        for chat_id, title in groups:
            try:
                # MUHIM: refresh_admins() xato bo'lsa False qaytaradi.
                # Ilgari javob o'qilmasdi va hisobot har doim "50/50" deb yolg'on yozardi.
                if await admin_manager.refresh_admins(context.bot, chat_id):
                    success += 1
                else:
                    failed += 1
            except Exception as e:
                failed += 1
                logger.error(f"Failed to refresh admins for {title}: {e}")
            await asyncio.sleep(config.ADMIN_REFRESH_DELAY)

        stats = admin_manager.get_stats()
        result_text = (
            f"{'✅' if failed == 0 else '⚠️'} **ADMIN RO'YXATLARI YANGILANDI**\n\n"
            f"📊 Muvaffaqiyatli: {success}/{len(groups)}\n"
            f"❌ Xatolik: {failed}\n"
            f"👥 Keshdagi adminlar: {stats['total_admins']}"
        )
        if status_msg is not None:
            try:
                await status_msg.edit_text(result_text, parse_mode='Markdown')
            except TelegramError as e:
                logger.warning(f"Hisobotni tahrirlab bo'lmadi: {e}")
        logger.info(f"Manual admin refresh completed: {success}/{len(groups)} groups, {failed} xato")

    context.application.create_task(_job())


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Tuple[Optional[int], Optional[str]]:
    """Moderatsiya komandasi nishonini aniqlaydi: reply qilingan user yoki argument (ID/@username)."""
    message = update.effective_message
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, get_user_mention(u)
    if context.args:
        arg = context.args[0].lstrip('@')
        if _INT_RE.fullmatch(arg):
            return int(arg), f"`{arg}`"
        try:
            async with db.get_connection() as conn:
                # Telegram username'lari katta-kichik harfni farqlamaydi.
                # Ilgari "WHERE username = ?" edi va /unban @Ali bazadagi "ali" ni topmasdi.
                row = await (await conn.execute(
                    'SELECT user_id FROM users WHERE username = ? COLLATE NOCASE', (arg,))).fetchone()
                if row:
                    return row['user_id'], f"@{safe_md(arg)}"
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
        await safe_reply(message, "❌ Bu komanda faqat adminlar uchun.")
        return False
    return True


@error_handler
async def unwarn_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    message = update.effective_message
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await safe_reply(message, "ℹ️ Foydalanish: xabarga **reply** qilib `/unwarn` yoki `/unwarn <ID|@username>`")
        return
    # Faqat SHU guruhdagi ogohlantirishlar tozalanadi
    await db.reset_warnings(target_id, chat_id=message.chat.id)
    await safe_reply(message, f"✅ {mention} — bu guruhdagi ogohlantirishlari tozalandi.")


@error_handler
async def unban_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    message = update.effective_message
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await safe_reply(message, "ℹ️ Foydalanish: `/unban <ID|@username>` yoki xabarga reply qiling")
        return
    chat = message.chat
    # Ilgari xato bo'lsa ham "✅ bani olib tashlandi" deb yozilardi.
    try:
        await context.bot.unban_chat_member(chat.id, target_id, only_if_banned=True)
    except TelegramError as e:
        logger.error(f"unban failed: {e}")
        await safe_reply(message, f"❌ {mention} — Telegram banni olmadi: `{safe_md(str(e)[:120])}`")
        return
    await db.unban_user(target_id)
    await db.reset_warnings(target_id, chat_id=chat.id)
    await safe_reply(message, f"✅ {mention} — bani olib tashlandi.")


@error_handler
async def unmute_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _moderation_guard(update, context):
        return
    message = update.effective_message
    target_id, mention = await _resolve_target(update, context)
    if not target_id:
        await safe_reply(message, "ℹ️ Foydalanish: xabarga **reply** qilib `/unmute` yoki `/unmute <ID|@username>`")
        return
    chat = message.chat

    # Bot API 6.5 dan keyin huquqlar alohida-alohida bo'ldi: berilmagan huquq
    # "taqiqlangan" deb olinadi. Ilgari faqat 4 tasi berilardi va odam
    # matn yozardi, lekin rasm/video/fayl yuborolmasdi — ya'ni yarim mute'da qolardi.
    permissions = None
    try:
        info = await context.bot.get_chat(chat.id)
        permissions = getattr(info, 'permissions', None)
    except TelegramError as e:
        logger.debug(f"get_chat({chat.id}) permissions olinmadi: {e}")

    if permissions is None:
        permissions = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True, can_react_to_messages=True)

    try:
        await context.bot.restrict_chat_member(chat.id, target_id, permissions)
    except TelegramError as e:
        logger.error(f"unmute failed: {e}")
        await safe_reply(message, "❌ Mute'ni ololmadim (bot huquqlarini tekshiring).")
        return
    await db.reset_warnings(target_id, chat_id=chat.id)
    await safe_reply(message, f"✅ {mention} — mute olib tashlandi.")


@error_handler
async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.from_user:
        return
    if not is_bot_admin(update.message.from_user.id):
        return

    stopped = False
    if context.user_data.get('broadcast_running'):
        # Ketayotgan tarqatishni ham to'xtatamiz. Ilgari faqat rejim tozalanardi
        # va boshlanib ketgan broadcast'ni hech qanday yo'l bilan to'xtatib bo'lmasdi.
        context.user_data['broadcast_cancel'] = True
        stopped = True

    if context.user_data.get('broadcast_mode'):
        context.user_data['broadcast_mode'] = None
        stopped = True

    if stopped:
        await safe_reply(update.message, "❌ Amal bekor qilindi.")


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

    await db.update_user(user.id, user.username, user.first_name, count_message=False)
    # Shaxsiy chatga yozgan — botni ochgan, broadcast yetib boradigan user
    await db.mark_user_started(user.id, user.username, user.first_name)

    if not is_bot_admin(user.id):
        # KUNLIK CHEK: bir odam adminning chatini kun bo'yi to'ldirib tashlamasin
        today = get_uz_time().strftime('%Y-%m-%d')
        day, count = _pm_daily.get(user.id, (today, 0))
        if day != today:
            count = 0
        if count >= config.PM_DAILY_LIMIT:
            _pm_daily[user.id] = (today, count + 1)
            if count == config.PM_DAILY_LIMIT:
                await safe_reply(message, "ℹ️ Bugunlik xabar chegarasiga yetdingiz. Ertaga davom etamiz.")
            logger.warning(f"🚫 PM KUNLIK CHEK: user {user.id} xabari yuborilmadi")
            return
        _pm_daily[user.id] = (today, count + 1)

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
    context.user_data['broadcast_cancel'] = False
    context.application.create_task(
        run_broadcast_task(context.bot, update.message.chat_id, update.message.message_id,
                           broadcast_mode, context.user_data))

async def run_broadcast_task(bot, from_chat_id, message_id, broadcast_mode, user_data):
    try:
        await _run_broadcast(bot, from_chat_id, message_id, broadcast_mode, user_data)
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
            user_data['broadcast_cancel'] = False


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
        f"`{safe_md(reason[:200])}`"
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


def _broadcast_cancelled(user_data) -> bool:
    return bool(user_data is not None and user_data.get('broadcast_cancel'))


async def _run_broadcast(bot, from_chat_id, message_id, broadcast_mode, user_data=None):
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
    cancelled = False

    for index, (chat_id, title) in enumerate(groups, 1):
        if _broadcast_cancelled(user_data):
            cancelled = True
            break
        try:
            await _broadcast_send(bot, chat_id, from_chat_id, message_id)
            success_count += 1
            logger.info(f"Broadcast sent to group {title} ({chat_id})")
        except (Forbidden, BadRequest) as e:
            kind = _classify_broadcast_error(e)
            fail_count += 1
            failed_list.append(('group', title, str(e), kind))
            if kind == 'source':
                abort_reason = str(e)
                logger.error(f"🛑 Broadcast to'xtatildi — manba xabarda muammo: {e}")
                break
            if kind == 'dead':
                await db.deactivate_group(chat_id)
            logger.warning(f"Failed to send to group {title} ({kind}): {e}")
        except TelegramError as e:
            fail_count += 1
            failed_list.append(('group', title, str(e), 'transient'))
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
        if _broadcast_cancelled(user_data):
            cancelled = True
            break
        try:
            await _broadcast_send(bot, user_id, from_chat_id, message_id)
            success_count += 1
            # Yetdi — 'reachable' deb belgilaymiz (keyingi broadcastlarda tez topiladi)
            await db.set_reachable(user_id, True)
            logger.info(f"Broadcast sent to user {username or first_name} ({user_id})")
        except (Forbidden, BadRequest) as e:
            kind = _classify_broadcast_error(e)
            fail_count += 1
            failed_list.append(('user', username or first_name or str(user_id), str(e), kind))
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
            failed_list.append(('user', username or first_name or str(user_id), str(e), 'transient'))
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

    head = "🛑 **XABAR TARQATISH BEKOR QILINDI**" if cancelled else "✅ **XABAR YUBORILDI**"
    result_text = f"{head}\n\n📊 Jami: **{total}**\n✅ Muvaffaqiyatli: **{success_count}**\n❌ Xatolik: **{fail_count}**\n\n"

    if broadcast_mode == 'all':
        result_text += f"📢 Guruhlar: **{len(groups)}** ta\n👥 Foydalanuvchilar: **{len(users)}** ta\n\n"

    result_text += f"🕐 {format_uz_time()}"

    if failed_list:
        result_text += f"\n\n❌ **Xatolik bo'lganlar ({len(failed_list)} ta):**\n"

        # Sabab xato MATNIDAN emas, tasniflangan TURIDAN olinadi.
        # Ilgari "'Forbidden' in error" tekshirilardi, lekin str(Forbidden(...)) ichida
        # "Forbidden" so'zi yo'q — shuning uchun har doim "Xatolik" chiqardi.
        for type_, name, error, kind in failed_list[:15]:
            emoji = "📢" if type_ == 'group' else "👤"
            name_escaped = safe_md(name[:30])
            low = error.lower()

            if 'blocked by the user' in low:
                reason = "Bot bloklangan"
            elif 'bot was kicked' in low or 'bot is not a member' in low:
                reason = "Bot guruhdan chiqarilgan"
            elif 'chat not found' in low or 'user not found' in low:
                reason = "Topilmadi"
            elif 'deactivated' in low:
                reason = "Akkaunt o'chirilgan"
            elif kind == 'dead':
                reason = "Yetib bormadi"
            else:
                reason = "Vaqtinchalik xato"

            result_text += f"{emoji} {name_escaped} - {reason}\n"

        if len(failed_list) > 15:
            result_text += f"\n... va yana {len(failed_list) - 15} ta"

    await status_message.edit_text(result_text, parse_mode='Markdown')
    logger.info(f"Broadcast completed: {success_count}/{total} successful")


@error_handler
async def group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Guruh xabarlarini tekshiradi (yangi VA tahrirlangan, KOMANDALAR ham).

    Bu handler -1 guruhda ro'yxatdan o'tadi — ya'ni boshqa handlerlardan OLDIN
    ishlaydi. Shuning uchun "/reklama http://spam.com" kabi xabar ham tekshiriladi.
    Xabar o'chirilsa ApplicationHandlerStop ko'tariladi va qolgan handlerlar
    (masalan /start javobi) o'chirilgan xabarga javob bermaydi.
    """
    message = update.effective_message
    if not message or not message.from_user:
        return

    user = message.from_user
    chat = message.chat
    message_text = message.text or message.caption or "[media]"

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
            return
        if await _handle_foreign_channel_message(context, message):
            raise ApplicationHandlerStop
        return

    is_admin, verification_method = await is_definitely_admin(context, chat.id, user.id, user)

    if is_admin:
        logger.info(f"✅ ADMIN BYPASS: user_id={user.id}, username={user.username}, "
                    f"is_bot={user.is_bot}, chat={chat.id}, method={verification_method}")
        await db.update_user(user.id, user.username, user.first_name, bump_total=True)
        return

    # FAIL-OPEN: admin tekshiruvi tarmoq/API xatosi tufayli AMALGA OSHMAGAN bo'lsa,
    # foydalanuvchini jazolamaymiz. Noaniqlikda admin foydasiga ishlaymiz.
    if verification_method == "verification_failed":
        logger.warning(
            f"⚠️ SKIP MODERATION: user {user.id} chat {chat.id} — admin holati aniqlanmadi "
            f"(API xatosi). Xavfsizlik uchun jazo qo'llanmadi."
        )
        await db.update_user(user.id, user.username, user.first_name, bump_total=True)
        return

    logger.info(f"👤 REGULAR USER: user_id={user.id}, username={user.username}, "
                f"is_bot={user.is_bot}, chat={chat.id}, verification={verification_method}")

    await db.update_user(user.id, user.username, user.first_name, bump_total=True)

    # ---------- FLOOD ----------
    if await flood_control.check(user.id, is_bot=user.is_bot, is_admin=is_admin, chat_id=chat.id):
        logger.warning(f"🚫 FLOOD DETECTED: user {user.id} in chat {chat.id}")
        await db.log_activity(user.id, user.username, user.first_name, chat.id,
                              chat.title or "Unknown", message_text, "flood", "blocked")
        deleted = True
        try:
            await message.delete()
        except TelegramError as e:
            deleted = False
            logger.warning(f"Flood xabarini o'chirib bo'lmadi: {e}")

        warning_msg = await safe_reply(
            message,
            f"⚠️ **{get_user_mention(user)}**\n\n🚫 Flooding taqiqlanadi! Iltimos, sekinroq yozing.")
        if warning_msg is not None:
            _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id,
                             config.WARNING_DELETE_DELAY)
        if deleted:
            raise ApplicationHandlerStop
        return

    # ---------- REKLAMA / HAVOLA ----------
    has_link, link_reason = message_has_link(message)
    if not has_link:
        return

    logger.warning(f"🚫 SPAM LINK DETECTED: user {user.id} in chat {chat.id} ({link_reason})")

    deleted = True
    try:
        await message.delete()
    except TelegramError as del_error:
        deleted = False
        logger.warning(f"Could not delete spam message: {del_error}")

    await db.increment_stat('blocked_links')
    warn_count = await db.add_warning(chat.id, user.id, user.username, user.first_name)
    await db.log_activity(user.id, user.username, user.first_name, chat.id,
                          chat.title or "Unknown", message_text, "spam_link",
                          f"blocked_warn_{warn_count}")

    # MUHIM: JAZO XABARDAN OLDIN qo'llanadi.
    # Ilgari avval reply_text yuborilardi va Markdown buzilsa (masalan ism ichida "]")
    # kod xatoga uchib, ban/mute umuman bajarilmay qolardi.
    punishment = ""
    if warn_count <= 0:
        # Baza yozmadi — ogohlantirish soni noma'lum, jazo QO'LLANMAYDI
        logger.error(f"add_warning bazaga yozilmadi (user {user.id}) — jazo qo'llanmadi")
    elif warn_count >= config.MAX_WARNS:
        try:
            await context.bot.ban_chat_member(chat.id, user.id)
            await db.ban_user(user.id)
            await db.increment_stat('banned_users')
            punishment = " — BAN!"
            logger.info(f"🔨 User {user.id} BANNED from {chat.id}")
        except TelegramError as e:
            punishment = " (ban qilib bo'lmadi — bot huquqlarini tekshiring)"
            logger.error(f"Error banning user: {e}")
    elif warn_count == config.MAX_WARNS - 1:
        mute_until = get_uz_time() + timedelta(minutes=config.MUTE_DURATION_MINUTES)
        try:
            await context.bot.restrict_chat_member(
                chat.id, user.id, ChatPermissions(can_send_messages=False),
                until_date=int(mute_until.timestamp()))
            await db.increment_stat('muted_users')
            punishment = f" — {config.MUTE_DURATION_MINUTES} daqiqa mute!"
            logger.info(f"🔇 User {user.id} MUTED in {chat.id} for {config.MUTE_DURATION_MINUTES} min")
        except TelegramError as e:
            punishment = " (mute qilib bo'lmadi — bot huquqlarini tekshiring)"
            logger.error(f"Error muting user: {e}")

    user_mention = get_user_mention(user)
    group_title = safe_md(chat.title or "guruhda")
    if warn_count <= 0:
        text = (f"⚠️ **{user_mention}**\n\n🚫 Reklama o'chirildi!\n\n"
                f"⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!")
    else:
        head = "⛔" if warn_count >= config.MAX_WARNS else "⚠️"
        text = (f"{head} **{user_mention}**\n\n"
                f"🚫 **{warn_count}/{config.MAX_WARNS} OGOHLANTIRISH**{punishment}\n\n"
                f"⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!")

    warning_msg = await safe_reply(message, text)
    if warning_msg is not None:
        _schedule_delete(context, warning_msg.chat_id, warning_msg.message_id,
                         config.WARNING_DELETE_DELAY)

    if deleted:
        raise ApplicationHandlerStop


@error_handler
async def new_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.new_chat_members:
        return

    chat = update.message.chat
    group_title = safe_md(chat.title or "guruh")

    bot_id = await get_bot_id(context)
    bot_added = bot_id is not None and any(
        member.id == bot_id for member in update.message.new_chat_members
    )

    if bot_added:
        logger.info(f"🤖 Bot added to group {chat.id} - refreshing admin list")
        await admin_manager.refresh_admins(context.bot, chat.id)

        try:
            bot_member = await context.bot.get_chat_member(chat.id, bot_id)
            if bot_member.status == 'administrator':
                if bot_member.can_delete_messages and bot_member.can_restrict_members:
                    setup_text = ("✅ **Bot muvaffaqiyatli faollashtirildi!**\n\n"
                                  "🛡 Guruhingiz endi spam va reklamadan himoyalangan.\n\n"
                                  "**Bot qanday ishlaydi:**\n"
                                  "• Link va reklama avtomatik o'chiriladi\n"
                                  f"• {config.MAX_WARNS} marta ogohlantirish → ban\n"
                                  f"• Ogohlantirish {config.WARN_TTL_DAYS} kundan keyin kuyadi\n"
                                  "• Adminlar erkin yoza oladi")
                else:
                    setup_text = ("⚠️ **Bot admin huquqlari to'liq emas!**\n\n"
                                  "Guruh to'liq ishlashi uchun botga quyidagi huquqlarni bering:\n\n"
                                  "✅ **Delete messages**\n✅ **Ban users**\n✅ **Restrict members**")
            else:
                setup_text = ("❌ **Bot admin emas!**\n\n"
                              "⚠️ **MUHIM:** Guruh to'liq ishlashi uchun botni **ADMIN** qiling!")

            setup_msg = await safe_reply(update.message, setup_text)
            if setup_msg is not None:
                _schedule_delete(context, setup_msg.chat_id, setup_msg.message_id, 60)
        except TelegramError as e:
            logger.error(f"Error sending setup instructions: {e}")

    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue

        welcome_msg = await safe_reply(
            update.message,
            f"👋 Xush kelibsiz, **{get_user_mention(new_member)}**!\n\n"
            f"⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!\n\n"
            f"🛡 Bu guruh spam va reklamadan himoyalangan.")
        if welcome_msg is not None:
            _schedule_delete(context, welcome_msg.chat_id, welcome_msg.message_id, 15)

        # count_message=False: guruhga kirish "xabar" emas
        await db.update_user(new_member.id, new_member.username, new_member.first_name,
                            count_message=False)

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
            # Muddati o'tgan ogohlantirishlar ham tozalanadi
            warns = await db.cleanup_old_warnings()
            if warns:
                logger.info(f"🧹 Ogohlantirishlar: {warns} ta muddati o'tgan yozuv o'chirildi")

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
            for cache in (_group_refresh, _linked_chat, _group_start_reply):
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
    task = application.bot_data.get('initial_refresh_task')
    if task is not None and not task.done():
        # Ilgari bekor qilinmasdi va bot o'chganda "Task was destroyed" ogohlantirishi chiqardi
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

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

    # PTB v20+ da "Unauthorized" klassi YO'Q — u o'lik satr edi.
    # Haqiqiy kritik holat: token noto'g'ri yoki bekor qilingan.
    is_critical = error_type in ('InvalidToken',) or '401' in error_msg

    # Conflict = bir token ostida IKKI bot instansi ishlamoqda (deploy muammosi).
    if 'Conflict' in error_type:
        logger.error(f"🚨 CONFLICT: bir token ostida bir nechta bot instansi ishlayotgan bo'lishi mumkin! {error_msg}")
        return

    # Oddiy tarmoq/ulanish xatolari — adminni spamlamaymiz
    if error_type in ('NetworkError', 'TimedOut', 'ReadError', 'RemoteProtocolError'):
        logger.warning(f"⚠️ Oddiy tarmoq/ulanish xatosi: {error_type} - {error_msg}")
        return

    if is_critical:
        logger.error(f"🚨 KRITIK: {error_type} - {error_msg} — bot token/ruxsatini tekshiring!")

        global _critical_alert_ts
        now = asyncio.get_event_loop().time()
        if now - _critical_alert_ts < _CRITICAL_ALERT_INTERVAL:
            return
        _critical_alert_ts = now

        # DIQQAT: xato matni safe_md bilan tozalanadi va safe_send zaxira yo'li bor.
        # Ilgari matn qalqonlanmasdi: "message_id_invalid" kabi xatodagi "_" belgisi
        # tufayli ogohlantirishning O'ZI yuborilmay qolardi.
        alert_text = (
            f"🚨 **KRITIK BOT XATOLIGI**\n\n"
            f"⚠️ Turi: `{safe_md(error_type)}`\n"
            f"📝 Xabar: {safe_md(error_msg[:200])}\n"
            f"⏰ {format_uz_time()}\n\n"
            f"❗️ Bot ishlay olmayapti — token yoki ruxsatlarni darhol tekshiring."
        )
        if await safe_send(context.bot, config.ADMIN_ID, alert_text) is not None:
            logger.info(f"✅ Critical error alert sent: {error_type}")
        return

    # MAXFIYLIK: butun update obyektini logga yozmaymiz — unda xabar matni bo'ladi
    # va bu LOG_MESSAGE_TEXT=false va'dasiga qarshi. Faqat kim/qayerda yoziladi.
    where = "—"
    if isinstance(update, Update):
        chat = update.effective_chat
        user = update.effective_user
        where = f"update_id={update.update_id}, chat={chat.id if chat else '—'}, user={user.id if user else '—'}"
    logger.error(f"Handler xatosi ({where}): {error_type}", exc_info=error)


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
        # PARALLEL ISHLOV: default holatda PTB bir vaqtda FAQAT BITTA xabarni
        # qayta ishlaydi (max_concurrent_updates=1). get_chat_member kabi so'rovlar
        # 0.2-0.5 sekund olgani uchun butun bot sekundiga 2-5 xabarga tushib qolardi.
        .concurrent_updates(True)
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

    # MODERATSIYA — group=-1, ya'ni BARCHA boshqa handlerlardan OLDIN ishlaydi va
    # KOMANDALARNI HAM tekshiradi.
    # Ilgari "~filters.COMMAND" bor edi: "/reklama http://spam.com" kabi xabar
    # moderatsiyaga umuman tushmasdi — bu eng katta teshik edi.
    # Xizmat xabarlari (kirish/chiqish) chiqarib tashlanadi — ular uchun alohida handler bor.
    # Tahrirlangan (edited) xabarlar avtomatik qamraladi (effective_message).
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, group_message_handler),
        group=-1,
    )

    logger.info("✅ All handlers registered")
    logger.info("🟢 Bot is running...")
    logger.info("=" * 60)

    logger.info("🔄 Starting long-polling...")
    # drop_pending_updates=False (default): bot o'chganda kelgan xabarlar ham
    # ko'rib chiqiladi.
    # Webhook rejimi yo'q — PTB polling boshlanishida Telegram tomonidagi
    # webhook'ni o'zi o'chiradi, shuning uchun ziddiyat bo'lmaydi.
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