FROM golang:1.22-bookworm AS go-builder

WORKDIR /src
COPY h3/seckill_sender.go /src/h3/seckill_sender.go
RUN go build -o /src/seckill_sender /src/h3/seckill_sender.go

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

ENV TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    FC_SERVER_PORT=9000 \
    SECKILL_SENDER=go

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY h3/requirements.txt /app/h3/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/h3/requirements.txt \
    && python -m playwright install chromium

COPY h3 /app/h3
COPY fc /app/fc
COPY --from=go-builder /src/seckill_sender /app/h3/seckill_sender

EXPOSE 9000

CMD ["python", "/app/fc/handler.py"]
