FROM python:3.12-slim

# git + tar — для git archive ветки агента при воспроизведении
RUN apt-get update && apt-get install -y --no-install-recommends git tar \
    && rm -rf /var/lib/apt/lists/*
# sage работает от root, а /workspace принадлежит агентам (uid 10001) — без
# safe.directory git archive молча падал бы на dubious ownership
RUN git config --system --add safe.directory /workspace

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sage/ ./sage/

EXPOSE 8095
CMD ["uvicorn", "sage.app:app", "--host", "0.0.0.0", "--port", "8095"]
