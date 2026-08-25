FROM ghcr.io/astral-sh/uv:python3.14-alpine

ENV UV_PYTHON_DOWNLOADS=0
ENV UV_LINK_MODE=copy

COPY . /app
WORKDIR /app

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-editable
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

CMD ["uv", "run", "jabagram", "-c", "/data/config.ini", "-d", "/data/jabagram.db"]
VOLUME [ "/data" ]
