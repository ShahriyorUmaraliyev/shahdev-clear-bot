import logging
import re
import os
import sys
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
from telegram.error import Forbidden, BadRequest, TelegramError
from telegram.helpers import escape_markdown
from apscheduler.schedulers.asyncio import AsyncIOScheduler

UZ_TZ = ZoneInfo('Asia/Tashkent')


@dataclass
class Config:
    """Centralized bot configuration"""
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    PORT: int = int(os.getenv("PORT", "8080"))

    BASE_DIR: str = field(default_factory=lambda: os.path.dirname(os.path.abspath(__file__)))
    DB_FILE: str = field(init=False)

    MAX_WARNS: int = 3
    MUTE_DURATION_MINUTES: int = 10
    FLOOD_LIMIT: int = 3
    FLOOD_WINDOW_SECONDS: int = 5
    WARNING_DELETE_DELAY: int = 10
    BROADCAST_DELAY: float = 0.3
    FLOOD_CLEANUP_MINUTES: int = 5

    DB_TIMEOUT: int = 10

    LINK_PATTERNS: Tuple[re.Pattern, ...] = field(default_factory=tuple)

    def __post_init__(self):
        
        # DB_PATH env orqali persistent volume (Cloud Run/Docker) ga yo'naltirish mumkin.
        # Cloud Run'da lokal disk vaqtinchalik — baza yo'qolmasligi uchun volume ulang.
        self.DB_FILE = os.getenv("DB_PATH") or os.path.join(self.BASE_DIR, "bot_data.db")

        patterns = [
            r'http[s]?://(?:[a-zA-Z0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+',
            r'www\.[a-zA-Z0-9-]+\.[a-zA-Z]{2,}',
            r't\.me/[a-zA-Z0-9_+]+',
            r'(?:telegram\.me|telegram\.dog)/[a-zA-Z0-9_+]+',
            # @mention: kamida 5 ta belgi, oldi-orqasi harf/raqam bo'lmasligi (email ichidagi @ tutilmaydi)
            r'(?<![a-zA-Z0-9_@.])@[a-zA-Z][a-zA-Z0-9_]{4,}\b',
            # domen: oldida harf/raqam yo'q (versiya "2.5" yoki "file.com" so'z ichida tutilmaydi),
            # keyin ham qo'shimcha harf bo'lmasligi
            r'(?<![a-zA-Z0-9.@-])[a-zA-Z][a-zA-Z0-9-]{1,}\.(com|net|org|uz|ru|me|io|co|info|online|site|kg|tj)\b',
            r'bit\.ly/[a-zA-Z0-9]+',
            r'tinyurl\.com/[a-zA-Z0-9]+',
        ]
        self.LINK_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in patterns)

    def validate(self) -> bool:
        if not self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is required")
        if not self.ADMIN_ID:
            raise ValueError("ADMIN_ID is required")
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


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != config.ADMIN_ID:
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


class FloodControl:
    """Professional flood control without global locks"""

    def __init__(self, limit: int = 3, window: int = 5):
        self._data = {}
        self._limit = limit
        self._window = window

    async def check(self, user_id: int, is_bot: bool = False, is_admin: bool = False) -> bool:
        if is_admin or is_bot:
            return False

        now = asyncio.get_event_loop().time()

        if user_id not in self._data:
            self._data[user_id] = [now]
            return False

        self._data[user_id] = [t for t in self._data[user_id] if now - t < self._window]
        self._data[user_id].append(now)

        is_flooding = len(self._data[user_id]) > self._limit

        if is_flooding:
            logger.warning(f"🚫 FLOOD: User {user_id} sent {len(self._data[user_id])} messages in {self._window}s")

        return is_flooding

    async def cleanup(self) -> int:
        now = asyncio.get_event_loop().time()
        stale_keys = [
            uid for uid, times in self._data.items()
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
            admin_ids.add(config.ADMIN_ID)

            self._admins[chat_id] = admin_ids
            self._last_refresh[chat_id] = asyncio.get_event_loop().time()

            logger.info(f"🔄 Admin list refreshed for chat {chat_id}: {len(admin_ids)} admins")
            return True
        except Exception as e:
            logger.error(f"Failed to refresh admins for chat {chat_id}: {e}")
            return False
        finally:
            self._refreshing[chat_id] = False

    async def is_admin(self, context, chat_id: int, user_id: int) -> bool:
        if user_id == config.ADMIN_ID:
            return True

        if self._bot_id and user_id == self._bot_id:
            return True

        last_refresh = self._last_refresh.get(chat_id, 0)
        now = asyncio.get_event_loop().time()

        if now - last_refresh > self._refresh_interval:
            # Don't await here directly if we just want to trigger it in background
            # to prevent holding up the message, but PTB needs it.
            await self.refresh_admins(context.bot, chat_id)

        return user_id in self._admins.get(chat_id, set())

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

    # LAYER 0: Anonymous Admin Messages (Telegram official)
    if user.is_bot and user.username == "GroupAnonymousBot":
        logger.info(f"✅ LAYER 0: Anonymous admin message detected")
        return (True, "anonymous_admin")

    # LAYER 1: Bot owner
    if user_id == config.ADMIN_ID:
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
            except:
                pass

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

    @asynccontextmanager
    async def get_connection(self):
        async with aiosqlite.connect(self.db_path, timeout=config.DB_TIMEOUT) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute('PRAGMA journal_mode=WAL')
            await conn.execute('PRAGMA synchronous=NORMAL')
            yield conn

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
        except Exception as e:
            pass

    async def increment_stat(self, key: str, amount: int = 1) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE stats SET value = value + ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?', (amount, key))
                await conn.commit()
        except Exception as e:
            pass

    async def get_active_groups(self):
        try:
            async with self.get_connection() as conn:
                cursor = await conn.execute('SELECT chat_id, title FROM groups WHERE is_active = 1 ORDER BY title')
                rows = await cursor.fetchall()
                return [(row['chat_id'], row['title']) for row in rows]
        except Exception:
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
        except Exception:
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
        except Exception:
            pass

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
            pass

    async def unban_user(self, user_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE users SET is_banned = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception:
            pass

    async def reset_warnings(self, user_id: int) -> None:
        try:
            async with self.get_connection() as conn:
                await conn.execute('UPDATE users SET warn_count = 0, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?', (user_id,))
                await conn.commit()
        except Exception:
            pass

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
        except Exception:
            return {'users': 0, 'groups': 0, 'blocked_links': 0, 'banned_users': 0, 'total_messages': 0, 'muted_users': 0, 'total_warnings': 0, 'broadcasts_sent': 0}

    async def log_activity(self, user_id, username, first_name, chat_id, chat_title, message_text, message_type, action_taken):
        try:
            truncated_text = message_text[:500] if message_text else ""
            async with self.get_connection() as conn:
                await conn.execute('''
                    INSERT INTO activity_log
                    (user_id, username, first_name, chat_id, chat_title, message_text, message_type, action_taken)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, username, first_name, chat_id, chat_title, truncated_text, message_type, action_taken))
                await conn.commit()
        except Exception:
            pass

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
        except Exception:
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
        except Exception:
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


def contains_link(text: str) -> bool:
    if not text:
        return False
    return any(pattern.search(text) for pattern in config.LINK_PATTERNS)


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
        except TelegramError:
            pass

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

        if user.id == config.ADMIN_ID:
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

    if user.id == config.ADMIN_ID:
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
        msg_preview = act['message_text'][:30] + "..." if len(act['message_text']) > 30 else act['message_text']
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
        except Exception:
            pass
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
    if not update.message or update.message.from_user.id != config.ADMIN_ID:
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

    await db.update_user(user.id, user.username, user.first_name)
    # Shaxsiy chatga yozgan — botni ochgan, broadcast yetib boradigan user
    await db.mark_user_started(user.id, user.username, user.first_name)

    if user.id != config.ADMIN_ID:
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

    for index, (chat_id, title) in enumerate(groups, 1):
        try:
            await bot.copy_message(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id)
            success_count += 1
            logger.info(f"Broadcast sent to group {title} ({chat_id})")
        except (Forbidden, BadRequest) as e:
            fail_count += 1
            failed_list.append(('group', title, str(e)))
            await db.deactivate_group(chat_id)
            logger.warning(f"Failed to send to group {title}: {e}")
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

    for index, (user_id, username, first_name) in enumerate(users, len(groups) + 1):
        try:
            await bot.copy_message(chat_id=user_id, from_chat_id=from_chat_id, message_id=message_id)
            success_count += 1
            # Yetdi — 'reachable' deb belgilaymiz (keyingi broadcastlarda tez topiladi)
            await db.set_reachable(user_id, True)
            logger.info(f"Broadcast sent to user {username or first_name} ({user_id})")
        except (Forbidden, BadRequest) as e:
            fail_count += 1
            # Yetib bo'lmadi (bloklangan/start qilmagan) — 'o'lik' deb belgilaymiz, qayta urinilmaydi
            await db.set_reachable(user_id, False)
            failed_list.append(('user', username or first_name or str(user_id), str(e)))
            logger.warning(f"Failed to send to user {user_id}: {e}")
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

    if await flood_control.check(user.id, is_bot=user.is_bot, is_admin=is_admin):
        try:
            logger.warning(f"🚫 FLOOD DETECTED: user {user.id} in chat {chat.id}")

            await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", message_text, "flood", "blocked")
            await message.delete()

            user_mention = get_user_mention(user)
            warning_msg = await message.reply_text(f"⚠️ **{user_mention}**\n\n🚫 Flooding taqiqlanadi! Iltimos, sekinroq yozing.", parse_mode='Markdown')

            context.job_queue.run_once(delete_message_job, config.WARNING_DELETE_DELAY, data={'chat_id': warning_msg.chat_id, 'message_id': warning_msg.message_id})
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

            if warn_count >= config.MAX_WARNS:
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

            context.job_queue.run_once(delete_message_job, config.WARNING_DELETE_DELAY, data={'chat_id': warning_msg.chat_id, 'message_id': warning_msg.message_id})

        except TelegramError as e:
            logger.error(f"Error handling link spam: {e}")

        return

    await db.log_activity(user.id, user.username, user.first_name, chat.id, chat.title or "Unknown", message_text, "normal_message", "allowed")

    try:
        async with db.get_connection() as conn:
            cursor = await conn.execute('SELECT message_count FROM users WHERE user_id = ?', (user.id,))
            row = await cursor.fetchone()

            if row and row['message_count'] == 1:
                user_mention = get_user_mention(user)
                group_title = escape_markdown(chat.title or "guruh")

                welcome_msg = await message.reply_text(f"👋 Xush kelibsiz, **{user_mention}**!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!\n\n🛡 Bu guruh spam va reklamadan himoyalangan.", parse_mode='Markdown')

                context.job_queue.run_once(delete_message_job, 15, data={'chat_id': welcome_msg.chat_id, 'message_id': welcome_msg.message_id})

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

            context.job_queue.run_once(delete_message_job, 60, data={'chat_id': setup_msg.chat_id, 'message_id': setup_msg.message_id})

        except TelegramError as e:
            logger.error(f"Error sending setup instructions: {e}")

    for new_member in update.message.new_chat_members:
        if new_member.is_bot:
            continue

        user_mention = get_user_mention(new_member)

        try:
            welcome_msg = await update.message.reply_text(f"👋 Xush kelibsiz, **{user_mention}**!\n\n⚠️ Iltimos **\"{group_title}\"** guruhida reklama tarqatmang!\n\n🛡 Bu guruh spam va reklamadan himoyalangan.", parse_mode='Markdown')
            context.job_queue.run_once(delete_message_job, 15, data={'chat_id': welcome_msg.chat_id, 'message_id': welcome_msg.message_id})
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

        logger.info("🔄 Refreshing admin lists...")
        groups = await db.get_active_groups()
        refresh_count = 0
        for chat_id, title in groups:
            try:
                success = await admin_manager.refresh_admins(application.bot, chat_id)
                if success:
                    refresh_count += 1
            except Exception as e:
                logger.warning(f"Could not refresh admins for {title}: {e}")

        logger.info(f"✅ Admin lists refreshed for {refresh_count}/{len(groups)} groups")

        try:
            startup_msg = f"🚀 **BOT ISHGA TUSHDI**\n\n✅ Guruhlar: {len(groups)} ta\n✅ Adminlar: {refresh_count} guruhda yuklandi\n⏰ {format_uz_time()}\n\nBot muvaffaqiyatli ishga tushdi."
            await application.bot.send_message(chat_id=config.ADMIN_ID, text=startup_msg, parse_mode='Markdown')
            logger.info("✅ Startup notification sent")
        except Exception as e:
            logger.warning(f"Could not send startup notification: {e}")

        async def cleanup_activity_job():
            await db.cleanup_old_activity(days=7)

        async def refresh_all_admins():
            groups = await db.get_active_groups()
            active_ids = [chat_id for chat_id, _ in groups]
            for chat_id in active_ids:
                try:
                    await admin_manager.refresh_admins(application.bot, chat_id)
                except Exception:
                    pass
            # Memory leak oldini olish: o'lik guruhlar keshini tozalaymiz
            admin_manager.prune(active_ids)
            for cid in list(_group_refresh.keys()):
                if cid not in set(active_ids):
                    del _group_refresh[cid]

        scheduler = AsyncIOScheduler()
        scheduler.add_job(cleanup_flood_data, 'interval', minutes=config.FLOOD_CLEANUP_MINUTES)
        scheduler.add_job(cleanup_activity_job, 'interval', hours=24)
        scheduler.add_job(refresh_all_admins, 'interval', minutes=10)
        scheduler.start()

        # Scheduler garbage-collect bo'lib ketmasligi uchun application'da saqlaymiz
        application.bot_data['scheduler'] = scheduler

        logger.info("✅ Scheduler started")
        logger.info("✅ Bot initialization completed")

    except Exception as e:
        logger.exception(f"❌ Error during initialization: {e}")
        sys.exit(1)


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

    logger.info(f"✅ Admin ID: {config.ADMIN_ID}")
    logger.info(f"✅ Bot Username: {config.BOT_USERNAME}")
    logger.info(f"✅ Database: {config.DB_FILE}")
    logger.info("🔍 Bot will auto-clear conflicts on startup...")

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
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
            await application.bot.set_webhook(url=config.WEBHOOK_URL, allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

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
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped by user")
    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")
        sys.exit(1)