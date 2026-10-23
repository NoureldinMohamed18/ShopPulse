# ---- ShopPulse image: one image, several commands (api / dashboard / pipeline) ----
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHOPPULSE_DATA_DIR=/data

WORKDIR /app

# Install dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

# Never run as root inside a container.
RUN useradd --create-home appuser && mkdir /data && chown appuser /data
USER appuser
VOLUME /data

EXPOSE 5000 8050
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://localhost:5000/health', timeout=3)" || exit 1

# default: the REST API served by gunicorn (a production WSGI server, not Flask's dev server)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "shoppulse.wsgi:app"]
