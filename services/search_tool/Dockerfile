FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY search/ ./search/

EXPOSE 8070
CMD ["uvicorn", "search.app:app", "--host", "0.0.0.0", "--port", "8070"]
