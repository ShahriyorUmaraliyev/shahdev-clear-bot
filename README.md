# ShahDev Clear Bot

Telegram guruhlarini spam va reklamadan tozalaydigan bot.

## Nima qiladi

- Link, domen (subdomenlar bilan) va reklama xabarlarini avtomatik o'chiradi
- **Matn ostiga yashirilgan havolani** ham ushlaydi ("Bu yerni bosing" → spam.com)
- **Komandalarni ham tekshiradi**: `/reklama http://spam.com` ham o'chiriladi
- 3 marta ogohlantirish: 2-da 10 daqiqa mute, 3-da ban
- Ogohlantirishlar **har guruh uchun alohida** va 30 kundan keyin o'z-o'zidan kuyadi
- Takroriy reklama yuborgan kanal guruhdan bloklanadi (`ban_chat_sender_chat`)
- Flood nazorati (har guruh uchun alohida hisoblanadi)
- Adminlar erkin yozadi (3 qatlamli tekshiruv: kesh → API → anonim admin)
- Admin panel: statistika, guruhlar, foydalanuvchilar, xabar tarqatish
- Bazaning har kunlik avtomatik nusxasi

Adminlar erkin yozishi uchun bot guruhda **admin** bo'lishi va
**Delete messages** + **Ban users** huquqlariga ega bo'lishi shart.

## O'rnatish

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env     # va .env ni to'ldiring
venv/bin/python main.py
```

`.env` ichidagi majburiy qiymatlar: `BOT_TOKEN`, `ADMIN_ID`, `BOT_USERNAME`
(username **"@" belgisiz**). Qolgan sozlamalar `.env.example` da izohlangan.

## Server (24/7)

To'liq qo'llanma: [DEPLOY-HETZNER.md](DEPLOY-HETZNER.md) — alohida Linux
foydalanuvchi, alohida venv, systemd xizmati.

Bot **faqat long-polling** rejimida ishlaydi: hech qanday port ochilmaydi,
tashqi web-server kerak emas.

## Tekshirish

```bash
venv/bin/python tests/selftest.py
```

Telegram'ga ulanmaydi: link aniqlash, flood nazorati, broadcast xato
mantig'i, baza va backup lokal tekshiriladi.

## Moderatsiya komandalari (guruhda, adminlar uchun)

| Komanda | Vazifasi |
|---|---|
| `/unwarn` | shu guruhdagi ogohlantirishlarni tozalash |
| `/unban` | banni olib tashlash |
| `/unmute` | mute'ni olib tashlash (barcha huquqlar qaytariladi) |

Har biri xabarga **reply** qilib yoki `/unban <ID yoki @username>` shaklida ishlaydi.

## Bot egasi uchun (shaxsiy chatda)

| Komanda | Vazifasi |
|---|---|
| `/start` | panel va tugmalar |
| `/users` | foydalanuvchilar ro'yxati |
| `/activity [soat]` | moderatsiya jurnali (o'chirilgan spam/flood) |
| `/refreshadmins` | admin ro'yxatlarini qayta yuklash |
| `/cancel` | xabar tarqatishni bekor qilish |

## Kanal nomidan yuborilgan xabarlar

Telegram'da kanal yoki guruh nomidan yuborilgan xabarda haqiqiy yuboruvchi
ko'rsatilmaydi — o'rniga xizmat akkaunti turadi. Shuning uchun bunday
xabarlar alohida tekshiriladi va quyidagilar **o'chirilmaydi**:

- guruhning o'zi nomidan yozilgan xabar (anonim admin)
- guruhga bog'langan kanalning avtomatik forward qilingan posti
- bog'langan kanal nomidan yozilgan xabar
- `ALLOWED_CHANNELS` ro'yxatidagi kanallar

Boshqa kanal nomidan kelgan reklama o'chiriladi (kanalga ogohlantirish
berib bo'lmaydi, shuning uchun faqat xabar o'chadi).

## Moderatsiya qanday ishlaydi

Moderatsiya handleri `group=-1` da ro'yxatdan o'tadi — ya'ni barcha boshqa
handlerlardan **oldin** ishlaydi va komandalarni ham qamrab oladi. Xabar
o'chirilsa `ApplicationHandlerStop` ko'tariladi va qolgan handlerlar
o'chirilgan xabarga javob bermaydi.

Jazo (ban/mute) **ogohlantirish xabari yuborilishidan oldin** qo'llanadi —
shunday qilib xabar yuborishdagi har qanday xato jazoni bekor qilmaydi.
Foydalanuvchi ismi va guruh nomi `safe_md()` bilan tozalanadi: Markdown
orqali botning o'z xabariga havola joylash mumkin emas.

## Maxfiylik

Oddiy xabarlar bazaga **umuman yozilmaydi** — na matni, na yozuvi.
Jurnalga faqat moderatsiya hodisalari (o'chirilgan spam, flood) tushadi va
7 kundan keyin avtomatik o'chiriladi (`LOG_MESSAGE_TEXT=true` bilan barcha
xabar matnini yoqish mumkin).
