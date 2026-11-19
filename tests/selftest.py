"""Botning o'z-o'zini tekshiruvi (tashqi kutubxona kerak emas).

Ishga tushirish:
    venv/bin/python tests/selftest.py

Telegram'ga ULANMAYDI — faqat lokal mantiq va baza tekshiriladi.
"""
import os
import sys
import asyncio
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix='clearbot-selftest-')

os.environ['BOT_TOKEN'] = '123456:TEST'
os.environ['ADMIN_ID'] = '111111'
os.environ['ADMIN_IDS'] = '555, 666;777'
os.environ['BOT_USERNAME'] = '@test_bot'
os.environ['DB_PATH'] = os.path.join(TMP, 'test.db')
os.environ['BACKUP_DIR'] = os.path.join(TMP, 'backups')
os.environ['ALLOWED_CHANNELS'] = '@sedoy_parfyum, -1009999'

sys.path.insert(0, ROOT)
import main as m  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(('  PASS  ' if ok else '  FAIL  ') + name + ('' if ok else f'  (got={got!r} want={want!r})'))
    if not ok:
        fails.append(name)


def section(title):
    print('\n== ' + title + ' ==')


# --------------------------------------------------------------- 1. Config
section('Config')
check('BOT_USERNAME dan @ olib tashlanadi', m.config.BOT_USERNAME, 'test_bot')
check('ADMIN_IDS o\'qiladi', sorted(m.config.ADMIN_IDS), [555, 666, 777, 111111])
check('is_bot_admin(asosiy admin)', m.is_bot_admin(111111), True)
check('is_bot_admin(qo\'shimcha admin)', m.is_bot_admin(666), True)
check('is_bot_admin(begona)', m.is_bot_admin(42), False)
check('is_bot_admin(None)', m.is_bot_admin(None), False)
check('DROP_PENDING_UPDATES default=False', m.config.DROP_PENDING_UPDATES, False)
check('LOG_MESSAGE_TEXT default=False', m.config.LOG_MESSAGE_TEXT, False)
check('BLOCK_MENTIONS default=False', m.config.BLOCK_MENTIONS, False)

# --------------------------------------------------------------- 2. Link aniqlash
section('contains_link — BLOKLANISHI kerak')
for t in [
    'google.com ga kiring',
    'https://example.com/aksiya',
    't.me/mykanal',
    'www.spam.uz',
    'telegram.me/joinchat',
    'bit.ly/abc123',
    "Yangi kanalimizga obuna bo'ling @reklama_kanal",
    "@kanal1 @kanal2 @kanal3 hammasi zo'r",
    'Bepul kurs @ustozim da',
    'Kazino @stavkabot da pul ishla',
]:
    check(f'BLOCK  {t[:45]!r}', m.contains_link(t), True)

section('contains_link — BLOKLANMASLIGI kerak (noto\'g\'ri ban oldini olish)')
for t in [
    'Salom @Shahriyor qalaysan',
    '@admin yordam bering iltimos',
    '@Dilnoza rahmat sizga',
    "Narxi 5.000 so'm",
    'Men 2.5 kg oldim',
    'soat 10.00 da uchrashamiz',
    'ali@mail.ru manzilimga yoz',
    'versiya 1.0.net yangilandi',
    'Bugun havo issiq',
    '@ali va @vali kelishdi',
    '',
]:
    check(f'PASS   {t[:45]!r}', m.contains_link(t), False)

# --------------------------------------------------------------- 3. Broadcast xatolari
section('Broadcast xato klassifikatsiyasi')
from telegram.error import Forbidden, BadRequest  # noqa: E402

check('manba xabar topilmadi -> source',
      m._classify_broadcast_error(BadRequest('Message to copy not found')), 'source')
check('MESSAGE_ID_INVALID -> source',
      m._classify_broadcast_error(BadRequest('Bad Request: MESSAGE_ID_INVALID')), 'source')
check('xabar juda uzun -> source',
      m._classify_broadcast_error(BadRequest('Message is too long')), 'source')
check('chat not found -> dead',
      m._classify_broadcast_error(BadRequest('Chat not found')), 'dead')
check('bot kicked -> dead',
      m._classify_broadcast_error(Forbidden('Forbidden: bot was kicked from the group chat')), 'dead')
check('bot blocked -> dead',
      m._classify_broadcast_error(Forbidden('Forbidden: bot was blocked by the user')), 'dead')
check('noma\'lum xato -> transient',
      m._classify_broadcast_error(BadRequest('Something weird happened')), 'transient')
check('Internal Server Error -> transient',
      m._classify_broadcast_error(BadRequest('Internal Server Error')), 'transient')


# --------------------------------------------------------------- 3b. sender_chat
section('Kanal/anonim admin nomidan yuborilgan xabarlar')
from types import SimpleNamespace  # noqa: E402
from telegram.error import TelegramError  # noqa: E402

check('ALLOWED_CHANNELS username o\'qildi',
      'sedoy_parfyum' in m.config.ALLOWED_CHANNEL_USERNAMES, True)
check('ALLOWED_CHANNELS ID o\'qildi', -1009999 in m.config.ALLOWED_CHANNEL_IDS, True)


def _chat(cid, username=None, title=None):
    return SimpleNamespace(id=cid, username=username, title=title)


def _msg(chat, sender_chat=None, auto=False):
    return SimpleNamespace(chat=chat, sender_chat=sender_chat, is_automatic_forward=auto)


class _FakeBot:
    def __init__(self, linked=None, fail=False):
        self.linked = linked
        self.fail = fail
        self.calls = 0

    async def get_chat(self, chat_id):
        self.calls += 1
        if self.fail:
            raise TelegramError('get_chat ishlamadi')
        return SimpleNamespace(linked_chat_id=self.linked)


class _FakeMeBot:
    def __init__(self, fail=False):
        self.fail = fail
        self.me_calls = 0

    async def get_me(self):
        self.me_calls += 1
        if self.fail:
            raise TelegramError('get_me ishlamadi')
        return SimpleNamespace(id=8557212424)


def _ctx(bot):
    return SimpleNamespace(bot=bot)


async def sender_chat_tests():
    GROUP = _chat(-1001111, title='Mening guruhim')

    m._linked_chat.clear()
    check('sender_chat yo\'q -> oddiy tekshiruv',
          await m._check_sender_chat(_ctx(_FakeBot()), _msg(GROUP)), None)

    m._linked_chat.clear()
    check('guruh nomidan (anonim admin) -> RUXSAT',
          await m._check_sender_chat(_ctx(_FakeBot()), _msg(GROUP, sender_chat=_chat(-1001111))),
          (True, 'anonim_admin'))

    m._linked_chat.clear()
    check('bog\'langan kanal posti (avto-forward) -> RUXSAT',
          await m._check_sender_chat(_ctx(_FakeBot()), _msg(GROUP, sender_chat=_chat(-1002222), auto=True)),
          (True, 'boglangan_kanal_posti'))

    m._linked_chat.clear()
    check('ALLOWED_CHANNELS dagi kanal (@username) -> RUXSAT',
          await m._check_sender_chat(_ctx(_FakeBot()),
                                     _msg(GROUP, sender_chat=_chat(-1003333, username='sedoy_parfyum'))),
          (True, 'ruxsat_berilgan_kanal'))

    m._linked_chat.clear()
    check('ALLOWED_CHANNELS dagi kanal (ID) -> RUXSAT',
          await m._check_sender_chat(_ctx(_FakeBot()), _msg(GROUP, sender_chat=_chat(-1009999))),
          (True, 'ruxsat_berilgan_kanal'))

    m._linked_chat.clear()
    check('guruhga bog\'langan kanal nomidan -> RUXSAT',
          await m._check_sender_chat(_ctx(_FakeBot(linked=-1004444)),
                                     _msg(GROUP, sender_chat=_chat(-1004444))),
          (True, 'boglangan_kanal'))

    m._linked_chat.clear()
    check('begona kanal nomidan -> RUXSAT YO\'Q',
          await m._check_sender_chat(_ctx(_FakeBot(linked=-1004444)),
                                     _msg(GROUP, sender_chat=_chat(-1005555))),
          (False, 'begona_kanal'))

    # Kesh: get_chat soatiga bir marta chaqirilishi kerak
    m._linked_chat.clear()
    bot = _FakeBot(linked=-1004444)
    for _ in range(5):
        await m._check_sender_chat(_ctx(bot), _msg(GROUP, sender_chat=_chat(-1005555)))
    check('get_chat keshlanadi (5 xabar -> 1 so\'rov)', bot.calls, 1)

    # get_chat xato bersa kesh buzilmaydi va qayta uriniladi
    m._linked_chat.clear()
    badbot = _FakeBot(fail=True)
    res = await m._check_sender_chat(_ctx(badbot), _msg(GROUP, sender_chat=_chat(-1005555)))
    check('get_chat xatosi qulatmaydi', res, (False, 'begona_kanal'))
    check('xatoda kesh yozilmaydi (qayta uriniladi)', len(m._linked_chat), 0)


# --------------------------------------------------------------- 4. FloodControl
async def bot_id_tests():
    section('Bot ID keshlanadi (get_me bekorga chaqirilmaydi)')
    m.admin_manager._bot_id = None
    bot = _FakeMeBot()
    ctx = _ctx(bot)
    first = await m.get_bot_id(ctx)
    check('birinchi chaqiruvda ID olinadi', first, 8557212424)
    for _ in range(5):
        await m.get_bot_id(ctx)
    check('6 chaqiruv -> get_me faqat 1 marta', bot.me_calls, 1)

    m.admin_manager._bot_id = None
    bad = _FakeMeBot(fail=True)
    check('get_me xatosi qulatmaydi -> None', await m.get_bot_id(_ctx(bad)), None)
    m.admin_manager._bot_id = None


async def flood_tests():
    section('FloodControl (har guruh alohida hisoblanadi)')
    fc = m.FloodControl(limit=3, window=5)
    res = [await fc.check(1, chat_id=c) for c in (-100, -200, -300, -400)]
    check('4 xil guruhda bittadan xabar — flood EMAS', any(res), False)

    fc2 = m.FloodControl(limit=3, window=5)
    res2 = [await fc2.check(1, chat_id=-100) for _ in range(5)]
    check('bitta guruhda 5 xabar — flood', res2[-1], True)

    fc3 = m.FloodControl(limit=1, window=5)
    res3 = [await fc3.check(1, is_admin=True, chat_id=-100) for _ in range(5)]
    check('admin hech qachon flood emas', any(res3), False)

    fc4 = m.FloodControl(limit=1, window=5)
    res4 = [await fc4.check(1, is_bot=True, chat_id=-100) for _ in range(5)]
    check('bot hech qachon flood emas', any(res4), False)


# --------------------------------------------------------------- 5. Baza
async def db_tests():
    section('Baza: yozish / o\'qish')
    await m.db.init()
    await m.db.update_user(111, 'ali', 'Ali')
    await m.db.update_group(-100123, 'Test guruh', 50)

    check('add_warning 1-marta', await m.db.add_warning(111, 'ali', 'Ali'), 1)
    check('add_warning 2-marta', await m.db.add_warning(111, 'ali', 'Ali'), 2)

    await m.db.log_activity(111, 'ali', 'Ali', -100123, 'Test', 'shaxsiy gap', 'normal_message', 'allowed')
    await m.db.log_activity(111, 'ali', 'Ali', -100123, 'Test', 'google.com reklama', 'spam_link', 'blocked_warn_1')
    rows = await m.db.get_recent_activity(hours=1, limit=10)
    texts = {r['message_type']: r['message_text'] for r in rows}
    check('oddiy xabar matni SAQLANMAYDI (maxfiylik)', texts.get('normal_message'), '')
    check('spam xabar matni dalil sifatida saqlanadi', texts.get('spam_link'), 'google.com reklama')

    await m.db.set_stat('last_startup_notify', 1700000000)
    check('set_stat/get_stat', await m.db.get_stat('last_startup_notify'), 1700000000)
    check('get_stat yo\'q kalit -> default', await m.db.get_stat('yoq_kalit', 42), 42)

    check('faol guruh bor', len(await m.db.get_active_groups()), 1)
    await m.db.deactivate_group(-100123)
    check('faolsizlantirilgandan keyin 0 guruh', len(await m.db.get_active_groups()), 0)
    check('stats.users', (await m.db.get_stats())['users'], 1)

    section('Baza: bitta ulanish + parallel yozish')
    await asyncio.gather(*[m.db.update_user(200 + i % 7, f'u{i}', 'U') for i in range(300)])
    check('300 parallel yozish "database is locked" bermadi', True, True)

    section('Baza: backup va eski nusxalarni tozalash')
    first = await m.db.backup()
    check('backup fayl yaratildi', bool(first) and os.path.exists(first), True)
    check('backup bo\'sh emas', os.path.getsize(first) > 1000, True)

    for _ in range(m.config.BACKUP_KEEP + 5):
        await m.db.backup()
    kept = [f for f in os.listdir(m.config.BACKUP_DIR) if f.endswith('.db')]
    check(f'faqat {m.config.BACKUP_KEEP} ta nusxa qoldi', len(kept), m.config.BACKUP_KEEP)

    skipped = await m.db.backup(only_if_older_than=3600)
    check('yaqinda nusxa olingan bo\'lsa qayta olinmaydi', skipped, None)
    check('nusxalar soni o\'zgarmadi',
          len([f for f in os.listdir(m.config.BACKUP_DIR) if f.endswith('.db')]),
          m.config.BACKUP_KEEP)
    forced = await m.db.backup(only_if_older_than=0)
    check('eski bo\'lsa nusxa olinadi', bool(forced), True)

    await m.db.close()
    check('yopilgandan keyin qayta ulanadi', await m.db.get_stat('last_startup_notify'), 1700000000)
    await m.db.close()


# --------------------------------------------------------------- 6. Keshlar
async def cache_tests():
    section('Xotira keshlari tozalanadi')
    now = asyncio.get_event_loop().time()
    m._pm_notify_ts[1] = now - 999999
    m._pm_notify_ts[2] = now
    m._pm_flood_warn_ts[3] = now - 999999
    m._prune_pm_caches()
    check('eski yozuv o\'chdi', 1 in m._pm_notify_ts, False)
    check('yangi yozuv qoldi', 2 in m._pm_notify_ts, True)
    check('flood ogohlantirish keshi ham tozalandi', 3 in m._pm_flood_warn_ts, False)

    am = m.AdminManager()
    am._admins[-1] = {1}
    am._admins[-2] = {2}
    am._last_refresh[-1] = now
    am.prune([-1])
    check('o\'lik guruh admin keshidan o\'chdi', -2 in am._admins, False)
    check('faol guruh admin keshida qoldi', -1 in am._admins, True)
    check('is_fresh(yangi)', am.is_fresh(-1), True)
    check('is_fresh(bo\'sh)', am.is_fresh(-999), False)


async def main_async():
    await sender_chat_tests()
    await bot_id_tests()
    await flood_tests()
    await db_tests()
    await cache_tests()


asyncio.run(main_async())

# --------------------------------------------------------------- 7. Application
section('Application quriladi')
from telegram.ext import Application  # noqa: E402

app = (Application.builder().token('123456:TEST')
       .post_init(m.post_init).post_shutdown(m.post_shutdown).build())
check('job_queue mavjud (xabar avtomatik o\'chirish uchun shart)', app.job_queue is not None, True)

print('\n' + '=' * 55)
if fails:
    print(f'❌ {len(fails)} TEST YIQILDI:')
    for f in fails:
        print('   - ' + f)
    sys.exit(1)
print('✅ BARCHA TESTLAR O\'TDI')
