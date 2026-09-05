# Semora 0.3 is local until released. Compose supplies the sibling checkout by name.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY --from=semora packages/semora /semora/packages/semora
COPY --from=semora packages/semora-store /semora/packages/semora-store
COPY --from=semora packages/semora-store-pg /semora/packages/semora-store-pg

# Dependencies first, so editing console source does not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY scripts ./scripts
COPY README.md ./README.md
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

EXPOSE 8850

# 0.0.0.0, or a published port reaches nothing. One worker on purpose: two would contend
# for the same run, and arbitrating that is the runtime's lease, not something this
# image should paper over.
CMD ["uv", "run", "--no-sync", "uvicorn", "console.server:app", \
     "--host", "0.0.0.0", "--port", "8850"]
