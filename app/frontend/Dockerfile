# frontend
# The build context is the app folder, so paths start from app/.
FROM python:3.12-slim

WORKDIR /app

# Install the libraries first, so Docker can reuse this layer
# when only the code or the pages change.
COPY frontend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything the frontend needs: the code, the page templates, the CSS and the script.
COPY frontend/main.py .
COPY frontend/templates templates
COPY frontend/static static

# One Uvicorn worker per container.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
