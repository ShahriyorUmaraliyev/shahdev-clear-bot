FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Xavfsizlik: root sifatida emas, alohida foydalanuvchi ostida ishlaymiz
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

CMD ["python", "main.py"]
