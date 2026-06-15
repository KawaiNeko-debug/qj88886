# FC seckill test runtime

This directory is a minimal Alibaba Cloud Function Compute custom-container runtime for the immediate test workflow.

## Deploy

Build from the repository root so Docker can copy both `h3/` and `fc/`:

```bash
docker build -f fc/Dockerfile -t jlc-seckill-fc:latest .
```

Deploy the image to Function Compute as an HTTP-triggered custom container.

Recommended FC settings:

- Port: `9000`
- Function timeout: at least `1200` seconds
- HTTP trigger timeout: at least `1200` seconds
- Instance concurrency: `1`
- Maximum instances: at least the number of test accounts
- Environment variable: `FC_INVOKE_TOKEN=<same value as GitHub secret ALIYUN_FC_INVOKE_TOKEN>`

This gives each account its own function invocation and browser process. It does not guarantee a unique public egress IP for each account.

## GitHub test

Add these GitHub Actions secrets:

- `ALIYUN_FC_SECKILL_URL`: the FC HTTP trigger URL
- `ALIYUN_FC_INVOKE_TOKEN`: the Bearer token checked by `fc/handler.py`

Then manually run `seckill-fc-test-now`. By default it uses the first two lines of `ACCOUNTS_TEST`, logs in after 1 minute, starts seckill after 5 minutes, hard-stops after 6 minutes, then sends the xlsx through Telegram.

## Local HTTP check

Health check:

```bash
curl http://127.0.0.1:9000/health
```

Invoke with a local payload:

```bash
curl -X POST "$ALIYUN_FC_SECKILL_URL" \
  -H "Authorization: Bearer $ALIYUN_FC_INVOKE_TOKEN" \
  -H "Content-Type: application/json" \
  --data @fc/local_test_payload.example.json
```
