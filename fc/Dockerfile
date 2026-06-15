FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    FC_SERVER_PORT=9000 \
    SECKILL_SENDER=go

RUN apt-get update \
    && apt-get install -y --no-install-recommends golang-go tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY h3/requirements.txt /app/h3/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/h3/requirements.txt \
    && python -m playwright install chromium

COPY h3 /app/h3
COPY fc /app/fc

RUN go build -o /app/h3/seckill_sender /app/h3/seckill_sender.go

EXPOSE 9000

CMD ["python", "/app/fc/handler.py"]
