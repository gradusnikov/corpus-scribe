FROM python:3.12-slim

ENV XDG_CACHE_HOME=/tmp/.cache

# System deps for the Markdown -> HTML -> Chromium PDF pipeline
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    pandoc \
    fonts-liberation \
    fonts-freefont-otf \
    fonts-noto \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Output volume for saved PDFs and Markdown
VOLUME /output

EXPOSE 5000

CMD ["python", "main.py"]
