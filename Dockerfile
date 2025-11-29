# 1. Base image
FROM python:3.11-slim

# 2. Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libxdamage1 \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# 3. Create working directory
WORKDIR /app

# 4. Copy requirements
COPY requirements.txt /app/

# 5. Install Python packages
RUN pip install --no-cache-dir -r requirements.txt

# 6. Install Playwright + Chromium
RUN playwright install --with-deps chromium

# 7. Copy project files
COPY . /app/

# 8. Expose port
EXPOSE 8080

# 9. Set env vars (optional)
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

# 10. Start the FastAPI app with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]