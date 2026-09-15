FROM python:3.11-slim

# No pip dependencies: the backend is standard-library only.
WORKDIR /app

# Copy the whole app, not just the Python entry point. index.html pulls in
# styles.css, app.js, icons.js and assets/hero-vault.jpg -- if those are left
# out of the image the site loads as unstyled HTML with no working sign-in.
COPY . .

# /app/data is the mount point for the persistent disk (see render.yaml).
RUN mkdir -p /app/data && chmod +x /app/start.sh

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DB_PATH=/app/data/app.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["python3", "server.py"]
