FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    XV_PORT=8730 \
    XV_DATA_DIR=/data \
    XV_CACHE_MAX_MB=512

WORKDIR /app
COPY app.py xclient.py store.py ops.json ./

EXPOSE 8730
CMD ["python3", "app.py"]
