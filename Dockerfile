FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# These layers depend only on pyproject.toml, so application edits reuse them.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c "import tomllib, pathlib; deps = tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['dependencies']; pathlib.Path('/tmp/requirements.txt').write_text('\n'.join(deps) + '\n')" \
    && pip install -r /tmp/requirements.txt hatchling \
    && rm /tmp/requirements.txt

RUN arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) arch=x64 ;; \
      aarch64|arm64) arch=arm64 ;; \
    esac; \
    curl -fsSL -o .tailwindcss "https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-${arch}" \
    && chmod +x .tailwindcss

COPY . .
RUN chmod +x scripts/build-css.sh \
    && ./scripts/build-css.sh \
    && pip install --no-cache-dir --no-deps --no-build-isolation .

EXPOSE 8000
CMD ["stablehand-web"]
