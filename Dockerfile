FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN chmod +x scripts/build-css.sh \
    && ./scripts/build-css.sh \
    && pip install --no-cache-dir .

EXPOSE 8000
CMD ["stablehand-web"]
