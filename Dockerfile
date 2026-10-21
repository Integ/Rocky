FROM python:3.12-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home rocky \
    && mkdir -p /app/data && chown -R rocky:rocky /app/data
USER rocky
EXPOSE 8000
CMD ["rocky", "api"]
