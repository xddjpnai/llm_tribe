FROM python:3.12-slim

# tesseract-ocr + языковые пакеты (eng+rus) для OCR сканов литературы
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cpu_models/ ./cpu_models/

ENV MODELS_CACHE=/models
EXPOSE 8060
CMD ["uvicorn", "cpu_models.app:app", "--host", "0.0.0.0", "--port", "8060"]
