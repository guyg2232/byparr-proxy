FROM python:3.12-slim

WORKDIR /app
COPY byparr-proxy.py /app/

ENV UPSTREAM=https://1337x.to \
    BYPARR=http://byparr:8191/v1 \
    TIMEOUT_MS=120000 \
    PORT=8888

EXPOSE 8888

CMD ["python", "-u", "byparr-proxy.py"]
