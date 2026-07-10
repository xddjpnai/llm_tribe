FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY guard/ ./guard/

EXPOSE 8080
CMD ["uvicorn", "guard.app:app", "--host", "0.0.0.0", "--port", "8080"]
