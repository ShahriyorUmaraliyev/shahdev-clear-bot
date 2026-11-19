# ShahDev Clear Bot

Telegram guruhlarini spam va reklamadan tozalaydigan bot.

## Nima qiladi

- Link, domen va reklama xabarlarini avtomatik o'chiradi
- 3 marta ogohlantirish: 2-da 10 daqiqa mute, 3-da ban
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
foydalanuvchi, alohida venv, systemd xizmati, hech qanday port ochilmaydi.

Cloud Run varianti: [DEPLOY.md](DEPLOY.md) (baza vaqtinchalik — tavsiya etilmaydi).

## Tekshirish

```bash
venv/bin/python tests/selftest.py
```

Telegram'ga ulanmaydi: link aniqlash, flood nazorati, broadcast xato
mantig'i, baza va backup lokal tekshiriladi.

## Moderatsiya komandalari (guruhda, adminlar uchun)

| Komanda | Vazifasi |
|---|---|
| `/unwarn` | ogohlantirishlarni tozalash |
| `/unban` | banni olib tashlash |
| `/unmute` | mute'ni olib tashlash |

Har biri xabarga **reply** qilib yoki `/unban <ID yoki @username>` shaklida ishlaydi.

## Bot egasi uchun (shaxsiy chatda)

| Komanda | Vazifasi |
|---|---|
| `/start` | panel va tugmalar |
| `/users` | foydalanuvchilar ro'yxati |
| `/activity [soat]` | faoliyat jurnali |
| `/refreshadmins` | admin ro'yxatlarini qayta yuklash |
| `/cancel` | xabar tarqatishni bekor qilish |

## Maxfiylik

Oddiy xabarlar matni bazaga **yozilmaydi**. Faqat o'chirilgan spam/flood
xabarlari dalil sifatida 7 kun saqlanadi (`LOG_MESSAGE_TEXT=true` bilan
hammasini yoqish mumkin).
