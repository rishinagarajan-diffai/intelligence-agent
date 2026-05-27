FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sync the Playwright browser version with the installed library version.
# The base image ships a browser binary, but running install ensures the
# exact version matches what playwright>=1.40.0 resolves to at build time.
RUN python -m playwright install chromium

COPY . .

RUN printf '#!/bin/sh\nexec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}\n' > /start.sh && chmod +x /start.sh

EXPOSE 8000

ENTRYPOINT ["/start.sh"]
