# semora comes from PyPI like everything else; this image needs only this repository.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so editing console source does not reinstall the world.
# No cache mount: Cloud Build runs docker without BuildKit and refuses --mount, and a
# fresh build VM has no cache to reuse anyway. --no-cache keeps uv's downloads out of
# the layer instead of baking them into the image.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY src ./src
COPY scripts ./scripts
COPY README.md ./README.md
RUN uv sync --frozen --no-dev --no-cache

EXPOSE 8850

# 0.0.0.0, or a published port reaches nothing. One worker on purpose: two would contend
# for the same run, and arbitrating that is the runtime's lease, not something this
# image should paper over.
CMD ["uv", "run", "--no-sync", "uvicorn", "console.server:app", \
     "--host", "0.0.0.0", "--port", "8850"]
