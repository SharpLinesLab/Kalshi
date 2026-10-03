FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DB_PATH=/data/btc15.db KILL_FILE=/data/KILL
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 btc15 && mkdir /data && chown btc15 /data
USER btc15
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import socket;socket.create_connection(('127.0.0.1',8080),3)" || exit 1
# Credentials come from the environment / mounted key file, never from the image.
CMD ["btc15", "run"]
