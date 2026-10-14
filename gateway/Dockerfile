FROM astral/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS build
COPY --from=uv /uv /bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_DEV=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable && .venv/bin/python -c "import sys,zoneinfo; assert sys.version_info[:3] == (3,12,14); zoneinfo.ZoneInfo('America/New_York')"

FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY gateway ./gateway
COPY migrations ./migrations
COPY fixtures ./fixtures
COPY cube/bundles.json ./cube/bundles.json
COPY alembic.ini ./
USER 65532:65532
ENTRYPOINT ["python"]
CMD ["-m","uvicorn","gateway.runtime:app_factory","--factory","--host","0.0.0.0","--port","8000","--no-access-log","--no-proxy-headers","--timeout-graceful-shutdown","125"]
