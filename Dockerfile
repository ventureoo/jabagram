FROM python:3-alpine AS builder

ENV PATH="/root/.local/bin:${PATH}"
ENV PIPX_DEFAULT_PYTHON="/usr/local/bin/python3"
RUN python3 -m pip install --user pipx && python3 -m pipx ensurepath
RUN pipx install poetry && pipx inject poetry poetry-plugin-bundle
WORKDIR /app
COPY . ./
RUN poetry bundle venv --python=/usr/local/bin/python3 --only=main /venv

FROM python:3-alpine AS runner
COPY --from=builder /venv /venv
ENTRYPOINT ["/venv/bin/jabagram", "-c", "/data/config.ini", "-d", "/data/jabagram.db"]
VOLUME [ "/data" ]
