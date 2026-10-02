# Helper API image. Also used for the one-shot jobs: secrets, migrate, seed, login.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

FROM ghcr.io/astral-sh/uv:0.12.10 AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini /app/alembic.ini
COPY config /app/config
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# Build identity for /healthz; empty when the build is not from a git checkout.
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ENV OPSKIT_BUILD_COMMIT=$GIT_COMMIT OPSKIT_BUILD_BRANCH=$GIT_BRANCH
WORKDIR /app
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "opskit.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--no-server-header", "--no-proxy-headers"]

# Sample generators: same pinned Python, plus the samples dependency group (pinned Pillow).
FROM build AS samplegen
RUN uv sync --frozen --no-dev --group samples --no-editable
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /repo
ENTRYPOINT ["python", "-m", "tools.samplegen"]
