# ============================================================
#  Shahdev Clear Bot — Google Cloud Run deploy (PowerShell)
#  Reja: SQLite (instance diskida) + min-instances=1 (doim tirik)
#  Webhook rejimi — Cloud Run PORT'da tinglashi shart.
# ============================================================

# --- 1. O'ZGARTIRADIGAN QIYMATLAR -------------------------------------------
$PROJECT_ID  = "SIZNING_PROJECT_ID"          # gcloud projects list
$REGION      = "europe-west1"                # Toshkentga yaqin region
$SERVICE     = "shahdev-clear-bot"

# Bot maxfiy qiymatlari (.env dagi bilan bir xil):
$BOT_TOKEN    = "SIZNING_BOT_TOKEN"
$ADMIN_ID     = "SIZNING_ADMIN_ID"
$BOT_USERNAME = "SIZNING_BOT_USERNAME"       # @ siz, masalan: my_clear_bot
# ----------------------------------------------------------------------------

Write-Host "==> Loyiha tanlanmoqda: $PROJECT_ID"
gcloud config set project $PROJECT_ID

Write-Host "==> Kerakli API'lar yoqilmoqda..."
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# --- 2. BIRINCHI DEPLOY (WEBHOOK_URL hali noma'lum) -------------------------
# WEBHOOK_URL ga vaqtinchalik qiymat beramiz — shunda bot webhook serverni
# ishga tushiradi va PORT'da tinglaydi (Cloud Run health-check o'tadi).
Write-Host "==> Birinchi deploy (manba kodidan build qilinadi)..."
gcloud run deploy $SERVICE `
  --source . `
  --region $REGION `
  --platform managed `
  --allow-unauthenticated `
  --min-instances 1 `
  --max-instances 1 `
  --no-cpu-throttling `
  --port 8080 `
  --memory 512Mi `
  --timeout 3600 `
  --set-env-vars "BOT_TOKEN=$BOT_TOKEN,ADMIN_ID=$ADMIN_ID,BOT_USERNAME=$BOT_USERNAME,WEBHOOK_URL=https://placeholder.invalid"

# --- 3. HAQIQIY URL'NI OLISH -----------------------------------------------
$URL = (gcloud run services describe $SERVICE --region $REGION --format "value(status.url)")
Write-Host "==> Servis URL: $URL"

# --- 4. WEBHOOK_URL'NI HAQIQIY URL BILAN YANGILASH --------------------------
Write-Host "==> WEBHOOK_URL haqiqiy manzilga o'rnatilmoqda..."
gcloud run services update $SERVICE `
  --region $REGION `
  --update-env-vars "WEBHOOK_URL=$URL"

Write-Host ""
Write-Host "============================================================"
Write-Host " ✅ TAYYOR! Bot ishga tushdi: $URL"
Write-Host " Telegram'da botga /start yozib tekshiring."
Write-Host "============================================================"
