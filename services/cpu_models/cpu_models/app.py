"""Лёгкие self-hosted CPU-модели: эмбеддинги (ONNX) + OCR (Tesseract).

Это НЕ LLM — компактные классические модели, работают на CPU без GPU и без
платы за вызов, поэтому сервис вызывается агентами напрямую, минуя budget-guard.

Модели грузятся лениво при первом запросе и кэшируются в /models (volume), чтобы
не тянуть веса при каждом рестарте. Импорт модуля не требует тяжёлых зависимостей —
это позволяет тестировать HTTP-контракт с подменёнными бэкендами.
"""
from __future__ import annotations

import base64
import binascii
import io
import logging
import os
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("cpu-models")

app = FastAPI(title="cpu-models")

# multilingual-e5-small: 384-мерные эмбеддинги, ONNX, ~118M параметров — CPU-friendly.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-small")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "384"))
MODELS_CACHE = os.environ.get("MODELS_CACHE", "/models")


@lru_cache(maxsize=1)
def _embedder():
    """fastembed поверх ONNX Runtime. Ленивая инициализация + кэш весов."""
    from fastembed import TextEmbedding  # noqa: PLC0415

    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=MODELS_CACHE)


def _ocr_image(img_bytes: bytes) -> str:
    """Tesseract через pytesseract. PaddleOCR/docTR — альтернатива под сложные сканы,
    но тяжелее; для литературы/PDF-сканов Tesseract достаточно и легковеснее."""
    import pytesseract  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    lang = os.environ.get("OCR_LANG", "eng+rus")
    return pytesseract.image_to_string(Image.open(io.BytesIO(img_bytes)), lang=lang).strip()


class EmbedRequest(BaseModel):
    texts: list[str]


class OcrRequest(BaseModel):
    image_b64: str


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/embed")
def embed(req: EmbedRequest):
    if not req.texts:
        return {"vectors": [], "dim": EMBED_DIM}
    try:
        vectors = [v.tolist() for v in _embedder().embed(req.texts)]
    except Exception as e:  # noqa: BLE001
        log.exception("embed failed")
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    return {"vectors": vectors, "dim": len(vectors[0]) if vectors else EMBED_DIM}


@app.post("/v1/ocr")
def ocr(req: OcrRequest):
    try:
        img = base64.b64decode(req.image_b64, validate=True)
    except (binascii.Error, ValueError):
        return JSONResponse(status_code=400, content={"error": "невалидный base64"})
    try:
        text = _ocr_image(img)
    except Exception as e:  # noqa: BLE001
        log.exception("ocr failed")
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})
    return {"text": text}
