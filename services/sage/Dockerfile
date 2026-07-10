FROM python:3.12-slim

# git + tar — для git archive ветки агента при воспроизведении
RUN apt-get update && apt-get install -y --no-install-recommends git tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sage/ ./sage/

EXPOSE 8095
CMD ["uvicorn", "sage.app:app", "--host", "0.0.0.0", "--port", "8095"]
