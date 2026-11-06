# syntax=docker/dockerfile:1

ARG PYTHON_BASE_IMAGE=python:3.12.4-slim-bookworm@sha256:a3e58f9399353be051735f09be0316bfdeab571a5c6a24fd78b92df85bcb2d85

FROM ${PYTHON_BASE_IMAGE} AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/eventledger-venv

COPY pyproject.toml README.md ./
COPY app ./app

RUN /opt/eventledger-venv/bin/python -m pip install --no-cache-dir .


FROM ${PYTHON_BASE_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/eventledger-venv/bin:${PATH}" \
    HOME=/tmp

RUN groupadd --system --gid 10001 eventledger \
    && useradd --system --uid 10001 --gid eventledger \
        --home-dir /nonexistent --shell /usr/sbin/nologin eventledger

WORKDIR /app

COPY --from=builder /opt/eventledger-venv /opt/eventledger-venv
COPY --chown=eventledger:eventledger app ./app
COPY --chown=eventledger:eventledger migrations ./migrations
COPY --chown=eventledger:eventledger alembic.ini ./alembic.ini

USER eventledger:eventledger

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
