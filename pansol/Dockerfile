# Pansol site + meme tracker. Build from the repo root:
#   docker build -f pansol/Dockerfile -t pansol .
#   docker run -p 8080:8080 -v pansol-data:/data -e PANSOL_ADMIN_WALLETS=<address> pansol
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27" "solders>=0.21" "pyyaml>=6" \
    "psycopg[binary,pool]>=3.2"

COPY phantom/__init__.py phantom/config.py phantom/
COPY pansol pansol

ENV HOST=0.0.0.0 \
    PORT=8080 \
    PANSOL_DB=/data/pansol.db \
    FORWARDED_ALLOW_IPS=*
EXPOSE 8080
CMD ["python", "-m", "pansol"]
