# Hetzner serverga o'rnatish (24/7)

Shu qo'llanma botni **mavjud sayt bilan aralashmaydigan** holda o'rnatadi:
alohida Linux foydalanuvchi, alohida Python muhiti, alohida systemd xizmati,
va **hech qanday port ochilmaydi** (long-polling). Saytga tegilmaydi.

Hamma komandalar `root` sifatida bajariladi.

## 1. Alohida foydalanuvchi

```bash
useradd -m -d /opt/clearbot -s /bin/bash clearbot
```

## 2. Kodni yuklash

```bash
sudo -u clearbot git clone https://github.com/ShahriyorUmaraliyev/shahdev-clear-bot.git /opt/clearbot/bot
```

## 3. Python muhiti

```bash
sudo -u clearbot python3 -m venv /opt/clearbot/bot/venv
sudo -u clearbot /opt/clearbot/bot/venv/bin/pip install -r /opt/clearbot/bot/requirements.txt
```

## 4. Sozlamalar

```bash
sudo -u clearbot nano /opt/clearbot/bot/.env
```

Ichiga (namuna `.env.example` da):

```
BOT_TOKEN=@BotFather_dan_olingan_token
ADMIN_ID=sizning_telegram_id
BOT_USERNAME=bot_username_at_belgisiz
```

`WEBHOOK_URL` **yozilmaydi** — shuning uchun bot long-polling'da ishlaydi va
server portini ochmaydi. Faylni faqat o'zi o'qiydigan qilib qo'yamiz:

```bash
chmod 600 /opt/clearbot/bot/.env
```

## 5. systemd xizmati

```bash
cat > /etc/systemd/system/clearbot.service <<'UNIT'
[Unit]
Description=ShahDev Clear Bot (Telegram)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=clearbot
Group=clearbot
WorkingDirectory=/opt/clearbot/bot
ExecStart=/opt/clearbot/bot/venv/bin/python main.py
Restart=always
RestartSec=5

NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/opt/clearbot/bot

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now clearbot
```

`ProtectHome=yes` — bot `/home` ichini **ko'rmaydi** ham, ya'ni saytning
fayllariga yeta olmaydi. `ProtectSystem=strict` + `ReadWritePaths` — faqat
o'z papkasiga yozadi.

## 6. Tekshirish

```bash
systemctl status clearbot --no-pager
```

```bash
ss -tlnp | grep -c clearbot
```

Natija `0` bo'lishi kerak — bot hech qanday portni egallamaydi.

## Yangilash

```bash
cd /opt/clearbot/bot && sudo -u clearbot git pull && \
sudo -u clearbot venv/bin/pip install -q -r requirements.txt && \
systemctl restart clearbot && systemctl status clearbot --no-pager
```

## Log ko'rish

```bash
journalctl -u clearbot -f
```

## Backup

Bot har kuni 03:17 (Toshkent) da bazaning nusxasini
`/opt/clearbot/bot/backups/` ga oladi va oxirgi 7 tasini saqlaydi.
Tashqi diskka ham ko'chirish uchun:

```bash
scp -r root@SERVER_IP:/opt/clearbot/bot/backups ./
```

## Qancha resurs yeydi

| Resurs | Sarf |
|---|---|
| RAM | 45–80 MB |
| CPU | 1% dan kam |
| Disk | ~45 MB + baza (yiliga 1–10 MB) |
| Trafik | oyiga ~0.5–1 GB |

Hetzner Cloud'ning oylik trafik chegarasi (EU'da 20 TB) ga nisbatan
bu ~0.005% — sezilmaydi.
