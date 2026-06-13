# 🚀 Google Cloud Run'ga deploy — Qo'llanma

Reja: **SQLite + min-instances=1** (tez ishga tushadigan variant).

---

## ⚠️ Muhim ogohlantirish (baza haqida)

Bu variantda baza (`bot_data.db`) Cloud Run instance'ining **xotirasida** yashaydi:

- ✅ `min-instances=1` tufayli instance doim tirik — baza kunlik ishlashda saqlanadi.
- ❌ Lekin **har `deploy` (yangi versiya) yoki kamdan-kam crash'da baza o'chadi** — foydalanuvchilar, ogohlantirishlar, statistika nolga tushadi.

> Bot uzoq muddat durable bo'lishi kerak bo'lsa — keyinroq **Postgres (bepul Neon)** ga o'tish kerak. Ayting, o'tkazib beraman.

---

## 1. Tayyorgarlik (bir marta)

1. **Google Cloud SDK** o'rnating: https://cloud.google.com/sdk/docs/install
2. Login qiling:
   ```powershell
   gcloud auth login
   ```
3. GCP loyiha (project) yarating yoki tanlang:
   ```powershell
   gcloud projects list
   ```
   Loyiha hali bo'lmasa, Cloud Console'da yarating va **billing** (to'lov) yoqing
   (Cloud Run'ning bepul limiti bor, lekin billing akkaunt ulangan bo'lishi shart).

---

## 2. Deploy qilish

1. `deploy.ps1` faylini oching va yuqoridagi 4 ta qiymatni to'ldiring:
   - `$PROJECT_ID` — loyiha ID'si
   - `$BOT_TOKEN`, `$ADMIN_ID`, `$BOT_USERNAME` — `.env` dagilar bilan bir xil

2. Skriptni ishga tushiring:
   ```powershell
   ./deploy.ps1
   ```

Skript avtomatik: API'larni yoqadi → kodni build qiladi → deploy qiladi →
servis URL'ini oladi → `WEBHOOK_URL`ni o'rnatadi.

3. Tugagach Telegram'da botga **/start** yozing — javob kelsa, ishladi ✅

---

## 3. Nima qanday ishlaydi (texnik)

| Sozlama | Sabab |
|---------|-------|
| **Webhook rejimi** | Cloud Run konteyner PORT'da tinglashi shart. `WEBHOOK_URL` o'rnatilsa kod aiohttp serverni ishga tushiradi. |
| `--min-instances 1` | Instance doim tirik → scheduler (admin refresh, flood cleanup) va baza ishlaydi. |
| `--max-instances 1` | SQLite'ga faqat bitta yozuvchi — bir nechta instance bazani buzmaydi. |
| `--no-cpu-throttling` | Broadcast/delete vazifalari so'rovdan keyin ham CPU'da ishlaydi. |
| `--timeout 3600` | Uzoq broadcast'lar uzilmaydi. |
| `WEBHOOK_URL=placeholder` (1-deploy) | URL hali noma'lum — server tinglashi uchun vaqtinchalik qiymat, keyin haqiqiysiga almashtiriladi. |

---

## 4. Foydali komandalar

Loglarni ko'rish:
```powershell
gcloud run services logs read shahdev-clear-bot --region europe-west1 --limit 50
```

Yangilanishdan keyin qayta deploy (kodni o'zgartirsangiz):
```powershell
./deploy.ps1
```

Botni to'xtatish (instance'ni 0 ga tushirish):
```powershell
gcloud run services update shahdev-clear-bot --region europe-west1 --min-instances 0
```

Webhook holatini tekshirish:
```powershell
# <TOKEN> o'rniga bot tokeningiz
curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
```
