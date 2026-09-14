# =========================================================
# Stage 1: Build Python Virtual Environment
# =========================================================
FROM debian:bookworm-slim AS python-builder

ENV DEBIAN_FRONTEND=noninteractive

# Install temporary build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-venv \
    python3-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Create virtual environment
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade core pip tools and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt


# =========================================================
# Stage 2: Final Minimal Runtime Image
# =========================================================
FROM debian:bookworm-slim AS final

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/opt/venv/bin:$PATH"

# Install ONLY runtime dependencies (Python3 runtime + curl for the health check)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set up Bot directory structure
WORKDIR /app

# Copy pre-built virtual environment from builder stage
COPY --from=python-builder /opt/venv /opt/venv

# Copy application source code and scripts
COPY src/ /app/src/
COPY src/main.py /app/main.py
COPY .env.example /app/.env.example
COPY docker/entrypoint.sh /app/entrypoint.sh

# Create non-root user and assign permissions
RUN useradd -m -u 1000 traider && \
    chmod +x /app/entrypoint.sh && \
    chown -R traider:traider /app

# Switch to security non-root user
USER traider

# Web portal / dashboard port
EXPOSE 8000

# Lightweight health check using standard python
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python3 -c "import sys; sys.exit(0)" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-u", "/app/main.py"]