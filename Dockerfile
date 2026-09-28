FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/output

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY crawler.py .

RUN useradd --create-home --uid 10001 crawler \
    && mkdir -p /output \
    && chown crawler:crawler /output
USER crawler

VOLUME ["/output"]

CMD ["python", "crawler.py"]
