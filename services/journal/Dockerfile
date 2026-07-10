FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY journal/ ./journal/

EXPOSE 8050
CMD ["uvicorn", "journal.app:app", "--host", "0.0.0.0", "--port", "8050"]
