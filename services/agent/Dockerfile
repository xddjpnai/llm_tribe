FROM python:3.12-slim

# git нужен для инструмента git_commit (ветка агента в общем workspace)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/

# Непривилегированный пользователь; /private и /workspace монтируются как volumes
RUN useradd -m -u 10001 agent && mkdir -p /private /workspace && chown agent /private /workspace
USER agent

CMD ["python", "-m", "agent.main"]
