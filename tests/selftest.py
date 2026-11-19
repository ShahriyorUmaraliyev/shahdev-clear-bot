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

# ------------------------------------------------- 2b. Yashirin havola va komandalar
section('Yashirin havola (text_link) va komanda-bypass')

from telegram import MessageEntity  # noqa: E402
from telegram.ext import filters as _f, ApplicationHandlerStop  # noqa: E402
import datetime as _dt  # noqa: E402
from telegram import Update as _Upd, Message as _Msg, Chat as _Chat, User as _Usr  # noqa: E402


def _gmsg(text, entities=None, caption=None, caption_entities=None):
    return _Msg(message_id=1, date=_dt.datetime.now(_dt.timezone.utc),
                chat=_Chat(id=-100777, type='supergroup'),
                from_user=_Usr(id=9, first_name='S', is_bot=False),
                text=text, entities=entities or [],
                caption=caption, caption_entities=caption_entities or [])


hidden = _gmsg('Bu yerni bosing', entities=[
    MessageEntity(type='text_link', offset=0, length=14, url='http://spam.com')])
check('matnda link yo\'q, lekin YASHIRIN havola bor -> bloklanadi',
      m.message_has_link(hidden)[0], True)
check('toza matn -> o\'tadi', m.message_has_link(_gmsg('Salom qalaysan'))[0], False)
check('ochiq link matnda -> bloklanadi', m.message_has_link(_gmsg('http://spam.com'))[0], True)
cap = _gmsg(None, caption='Rasm', caption_entities=[
    MessageEntity(type='text_link', offset=0, length=4, url='http://spam.com')])
check('rasm izohidagi yashirin havola -> bloklanadi', m.message_has_link(cap)[0], True)

# Moderatsiya filtri: komandalar HAM tekshirilishi kerak
mod_filter = _f.ChatType.GROUPS & ~_f.StatusUpdate.ALL
cmd = _gmsg('/reklama http://spam.com', entities=[
    MessageEntity(type='bot_command', offset=0, length=8)])
check('"/reklama <link>" moderatsiyaga TUSHADI',
      bool(mod_filter.check_update(_Upd(update_id=1, message=cmd))), True)
check('tahrirlangan xabar ham moderatsiyaga tushadi',
      bool(mod_filter.check_update(_Upd(update_id=2, edited_message=cmd))), True)

# --------------------------------------------------------------- 2c. Markdown xavfsizligi
section('Markdown injection himoyasi')


class _EvilUser:
    username = None
    first_name = 'Ali](http://evil.com)'
    id = 7


mention = m.get_user_mention(_EvilUser())
check('ism ichidagi ")" olib tashlanadi', ')' in mention.split('](tg://')[0], False)
check('ism ichidagi "]" olib tashlanadi', ']' in mention.split('](tg://')[0], False)
check('havola injection ishlamaydi', 'evil.com)' in mention, False)
check('mention baribir ishlaydi', mention.endswith('(tg://user?id=7)'), True)


class _EvilUser2:
    username = 'ali_bek'
    first_name = 'Ali'
    id = 8


check('username dagi "_" qalqonlanadi', m.get_user_mention(_EvilUser2()), '@ali\\_bek')
check('safe_md guruh nomini tozalaydi', m.safe_md('Guruh](http://x.com)'), 'Guruhhttp://x.com')
check('plain_text Markdown belgilarini olib tashlaydi',
      m.plain_text('**Ali** _bek_ `kod`'), 'Ali bek kod')

# --------------------------------------------------------------- 2d. ENV tekshiruvi
section('ENV qiymatlarini tekshirish')
os.environ['TEST_IDS'] = '111, -222 ; --5 , 12a, 333'
check('noto\'g\'ri ID lar e\'tiborsiz qoldiriladi',
      sorted(m._env_ids('TEST_IDS')), [-222, 111, 333])

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
from telegram.error import TelegramError, BadRequest  # noqa: E402

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
    check('begona BOT ham flood hisobiga tushadi', res4[-1], True)

    fc5 = m.FloodControl(limit=1, window=5)
    res5 = [await fc5.check(1, is_bot=True, is_admin=True, chat_id=-100) for _ in range(5)]
    check('admin bot ozod', any(res5), False)


# --------------------------------------------------------------- 5. Baza
async def db_tests():
    section('Baza: yozish / o\'qish')
    await m.db.init()
    await m.db.update_user(111, 'ali', 'Ali')
    await m.db.update_group(-100123, 'Test guruh', 50)

    check('add_warning 1-marta', await m.db.add_warning(-100123, 111, 'ali', 'Ali'), 1)
    check('add_warning 2-marta', await m.db.add_warning(-100123, 111, 'ali', 'Ali'), 2)
    check('BOSHQA guruhda hisob 1 dan boshlanadi',
          await m.db.add_warning(-100999, 111, 'ali', 'Ali'), 1)
    check('get_warnings(1-guruh)', await m.db.get_warnings(-100123, 111), 2)
    check('get_warnings(2-guruh)', await m.db.get_warnings(-100999, 111), 1)
    await m.db.reset_warnings(111, chat_id=-100999)
    check('reset faqat bitta guruhga ta\'sir qiladi',
          (await m.db.get_warnings(-100123, 111), await m.db.get_warnings(-100999, 111)), (2, 0))

    # Muddati o'tgan ogohlantirish 1 dan boshlanadi
    async with m.db.get_connection() as conn:
        await conn.execute("UPDATE chat_warnings SET updated_at = datetime('now','-400 days') "
                           "WHERE chat_id = ? AND user_id = ?", (-100123, 111))
        await conn.commit()
    check('eski ogohlantirish ko\'rinmaydi', await m.db.get_warnings(-100123, 111), 0)
    check('muddati o\'tgach hisob 1 dan boshlanadi',
          await m.db.add_warning(-100123, 111, 'ali', 'Ali'), 1)
    async with m.db.get_connection() as conn:
        await conn.execute("UPDATE chat_warnings SET updated_at = datetime('now','-400 days')")
        await conn.commit()
    check('cleanup_old_warnings eskilarni o\'chiradi',
          await m.db.cleanup_old_warnings() >= 1, True)

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
    check('stats.subscribers (hali obuna emas)', (await m.db.get_stats())['subscribers'], 0)
    await m.db.mark_user_started(111, 'ali', 'Ali')
    check('mark_user_started -> obunachi', (await m.db.get_stats())['subscribers'], 1)
    await m.db.update_user(111, 'ali', 'Ali', count_message=False)
    async with m.db.get_connection() as conn:
        row = await (await conn.execute('SELECT message_count FROM users WHERE user_id = 111')).fetchone()
    check('count_message=False xabarni sanamaydi', row['message_count'], 1)

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


# ------------------------------------------- 6b. Uchidan-uchiga: moderatsiya qarori
class _FakeMsg:
    """reply_text() ni ataylab yiqitib ko'rish uchun soxta xabar."""

    def __init__(self, text, chat, user, reply_fails=False):
        self.text = text
        self.caption = None
        self.entities = []
        self.caption_entities = []
        self.chat = chat
        self.from_user = user
        self.sender_chat = None
        self.is_automatic_forward = False
        self.message_id = 1
        self.chat_id = chat.id
        self.deleted = False
        self.reply_fails = reply_fails
        self.replies = []

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, parse_mode=None, **kw):
        if self.reply_fails and parse_mode == 'Markdown':
            raise BadRequest("Can't parse entities: can't find end of the entity")
        self.replies.append(text)
        return SimpleNamespace(chat_id=self.chat.id, message_id=2)


class _ModBot:
    def __init__(self, admin_ids=()):
        self.admin_ids = set(admin_ids)
        self.banned = []
        self.muted = []

    async def get_chat_member_count(self, chat_id):
        return 10

    async def get_chat_administrators(self, chat_id):
        return [SimpleNamespace(user=SimpleNamespace(id=i)) for i in self.admin_ids]

    async def get_chat_member(self, chat_id, user_id):
        status = 'administrator' if user_id in self.admin_ids else 'member'
        return SimpleNamespace(status=status)

    async def ban_chat_member(self, chat_id, user_id):
        self.banned.append(user_id)

    async def restrict_chat_member(self, chat_id, user_id, permissions, until_date=None):
        self.muted.append(user_id)


async def moderation_e2e_tests():
    section('Uchidan-uchiga: moderatsiya qarori')
    await m.db.init()
    chat = SimpleNamespace(id=-100555, title='Sinov guruh', type='supergroup')
    spammer = SimpleNamespace(id=4242, username='spam', first_name='Sp', is_bot=False)
    admin = SimpleNamespace(id=777, username='boss', first_name='B', is_bot=False)

    bot = _ModBot(admin_ids=[admin.id])
    m.admin_manager._admins.clear()
    m.admin_manager._last_refresh.clear()
    m._group_refresh.clear()

    def ctx(b):
        return SimpleNamespace(bot=b, job_queue=None)

    # 1. "/reklama <link>" — komanda bo'lsa ham o'chirilishi SHART
    msg = _FakeMsg('/reklama http://spam.com', chat, spammer)
    upd = SimpleNamespace(effective_message=msg)
    try:
        await m.group_message_handler(upd, ctx(bot))
        stopped = False
    except ApplicationHandlerStop:
        stopped = True
    check('"/reklama <link>" o\'chirildi', msg.deleted, True)
    check('o\'chirilgandan keyin boshqa handlerlar to\'xtatiladi', stopped, True)
    check('1-ogohlantirish berildi', await m.db.get_warnings(chat.id, spammer.id), 1)
    check('ban hali yo\'q', bot.banned, [])

    # 2. 2-ogohlantirish -> MUTE
    msg2 = _FakeMsg('yana reklama shop.spam.com', chat, spammer)
    try:
        await m.group_message_handler(SimpleNamespace(effective_message=msg2), ctx(bot))
    except ApplicationHandlerStop:
        pass
    check('subdomen ham ushlandi', msg2.deleted, True)
    check('2-ogohlantirishda MUTE', bot.muted, [spammer.id])

    # 3. 3-ogohlantirish -> BAN, VA reply_text yiqilsa ham ban bajarilishi SHART
    msg3 = _FakeMsg('oxirgi reklama t.me/kanal', chat, spammer, reply_fails=True)
    try:
        await m.group_message_handler(SimpleNamespace(effective_message=msg3), ctx(bot))
    except ApplicationHandlerStop:
        pass
    check('3-ogohlantirishda BAN', bot.banned, [spammer.id])
    check('Markdown yiqilsa ham jazo bajarildi (tartib tuzatilgan)', bot.banned, [spammer.id])
    check('zaxira yo\'l bilan oddiy matn yuborildi', len(msg3.replies), 1)

    # 4. ADMIN xabari — hech narsa qilinmaydi
    amsg = _FakeMsg('mening saytim http://sedoyparfyum.uz', chat, admin)
    await m.group_message_handler(SimpleNamespace(effective_message=amsg), ctx(bot))
    check('admin linki O\'CHIRILMAYDI', amsg.deleted, False)

    # 5. Anonim admin (guruh nomidan) — o'chirilmaydi
    anon = _FakeMsg('reklama http://spam.com', chat, spammer)
    anon.sender_chat = SimpleNamespace(id=chat.id, username=None, title=chat.title)
    await m.group_message_handler(SimpleNamespace(effective_message=anon), ctx(bot))
    check('anonim admin posti O\'CHIRILMAYDI', anon.deleted, False)

    # 6. Yashirin havola — o'chiriladi
    hidden = _FakeMsg('Bu yerni bosing', chat, SimpleNamespace(
        id=5151, username=None, first_name='H', is_bot=False))
    hidden.entities = [SimpleNamespace(type='text_link', url='http://spam.com')]
    try:
        await m.group_message_handler(SimpleNamespace(effective_message=hidden), ctx(bot))
    except ApplicationHandlerStop:
        pass
    check('yashirin havola o\'chirildi', hidden.deleted, True)

    await m.db.reset_warnings(spammer.id)
    # aiosqlite ulanishi yopilmasa, uning foniy oqimi jarayonni tirik ushlab turadi
    await m.db.close()


async def main_async():
    await sender_chat_tests()
    await bot_id_tests()
    await flood_tests()
    await db_tests()
    await cache_tests()
    await moderation_e2e_tests()


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
