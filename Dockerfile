# The console resolves semora by path from the neighbouring checkout, so that checkout
# arrives as a named build context rather than by widening this one to the parent
# directory. compose.yaml wires it; by hand it is:
#
#     docker build --build-context semora=../semora -t nexora-console .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /workspace

# The path dependencies have to be on disk before uv reads the lock file. Their layout
# has to match `[tool.uv.sources]` in the console's pyproject: ../semora/packages.
COPY --from=semora pyproject.toml uv.lock ./semora/
COPY --from=semora packages ./semora/packages

WORKDIR /workspace/nexora-console

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
