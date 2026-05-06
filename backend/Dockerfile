# ================================================================
# Aso Luxe Backend — Dockerfile
# Builds a container for Google Cloud Run deployment
# ================================================================

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Cloud Run injects PORT environment variable (default 8080)
ENV PORT=8080

# Initialise DB tables and start with gunicorn
# gunicorn is production-ready; never use flask dev server in production
CMD ["sh", "-c", "python -c 'from db import init_db; init_db()' && gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 60 app:app"]
