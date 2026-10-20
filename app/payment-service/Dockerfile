FROM python:3.12-slim

WORKDIR /app
COPY payment-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY payment-service/main.py .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
