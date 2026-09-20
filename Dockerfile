FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PARKING_DATA_DIR=/data
USER root
RUN apt-get update && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify openbox \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python3 -m venv --without-pip /opt/venv \
    && pip --python /opt/venv install --no-cache-dir . \
    && mkdir /data && chown pwuser:pwuser /data && chmod 700 /data
ENV PATH="/opt/venv/bin:$PATH"
USER pwuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD parking-bot health
ENTRYPOINT ["parking-bot"]
CMD ["run"]
