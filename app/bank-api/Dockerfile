# bank-api, given ready-made.
# The build context is the app folder, so paths start from app/.
FROM python:3.12-slim

WORKDIR /app

COPY bank-api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN opentelemetry-bootstrap -a install

COPY bank-api/main.py .

# One Uvicorn worker, one copy.
CMD ["opentelemetry-instrument", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
 