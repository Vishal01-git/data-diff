# ==============================================================================
# Multi-Stage Production Dockerfile for Enterprise Data-Validator CLI
# ==============================================================================

# Stage 1: Build stage with compiler & ODBC development headers
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

# Install build tools and ODBC development libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    unixodbc-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install build tooling
RUN pip install --no-cache-dir --upgrade pip setuptools wheel hatchling

# Copy source tree
COPY pyproject.toml README.md ./
COPY data_diff ./data_diff
COPY validator ./validator
COPY configs ./configs

# Build wheel package
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels .

# ------------------------------------------------------------------------------
# Stage 2: Minimal runtime image
# ------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runner

LABEL maintainer="Enterprise Data Platform Engineering" \
      description="Enterprise Cross-Database Data Validation & Diff Engine for SIT Migration" \
      version="0.12.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/home/appuser/.local/bin:${PATH}"

WORKDIR /app

# Install runtime ODBC libraries and CA certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    unixodbc \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged application user
RUN groupadd -r appuser -g 10001 && \
    useradd -r -u 10001 -g appuser -m -d /home/appuser -s /sbin/nologin appuser

# Copy wheels from builder stage
COPY --from=builder /build/wheels /tmp/wheels
COPY --from=builder /build/pyproject.toml /build/README.md /tmp/src/
COPY --from=builder /build/data_diff /tmp/src/data_diff
COPY --from=builder /build/validator /tmp/src/validator
COPY --from=builder /build/configs /tmp/src/configs

# Install data-validator with enterprise cloud database dialects
RUN pip install --no-cache-dir /tmp/wheels/*.whl \
    && pip install --no-cache-dir \
       duckdb \
       pydantic \
       rich \
       typer \
       pyyaml \
       jinja2 \
    && rm -rf /tmp/wheels /tmp/src

# Set up runtime directories with proper permissions
RUN mkdir -p /app/reports /app/.validator_state /app/configs && \
    chown -R appuser:appuser /app /home/appuser

# Switch to unprivileged user
USER appuser

# Expose reports directory as a volume
VOLUME ["/app/reports", "/app/configs"]

# Set CLI entrypoint
ENTRYPOINT ["data-validator"]
CMD ["--help"]
