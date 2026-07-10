FROM python:3.12-slim

# git — движок применения diff; docker-cli не нужен, ходим через SDK по docker.sock
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY selfmod/ ./selfmod/

EXPOSE 8090
CMD ["uvicorn", "selfmod.app:app", "--host", "0.0.0.0", "--port", "8090"]
