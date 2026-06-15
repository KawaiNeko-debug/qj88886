import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

ALLOWED_ENV_KEYS = {
    "PASSPORT_URL",
    "SLIDER_ID",
    "WRAPPER_ID",
    "BASE_URL",
    "SECKILL_BASE_URL",
    "SECKILL_CATEGORY_ACCESS_ID",
    "SECKILL_REFERER",
    "REFERER",
    "JLC_REFERER",
    "HEADER_CLIENT_TYPE",
    "HEADER_ACCESS_TOKEN",
    "HEADER_ACCESS_TOKEN_FALLBACKS",
    "HEADER_SECRET_KEY",
    "HEADER_XSRF_TOKEN",
    "TOKEN_KEY",
    "TOKEN_ALTERNATIVE_KEYS",
    "JLC_CLIENT_TYPE",
    "JLC_SECRET_KEY_VALUE",
    "JLC_USE_HTTP2",
    "JLC_ACCEPT_LANGUAGE",
    "JLC_CAS_APP_ID",
    "JLC_BIND_TOKEN_ENABLED",
    "JLC_MP_APPID",
    "JLC_MP_PAGE_VERSION",
    "JLC_MP_VERSION",
    "JLC_MP_ENV",
    "JLC_USER_AGENT",
    "BROWSER_VIEWPORT_WIDTH",
    "BROWSER_VIEWPORT_HEIGHT",
    "BROWSER_DEVICE_SCALE_FACTOR",
    "SECKILL_SOURCE",
    "SECKILL_PREWARM_CONCURRENCY",
    "SECKILL_BURST_CONCURRENCY",
    "SECKILL_BURST_INTERVAL_MS",
    "SECKILL_PREWARM_SERVER_ARRIVAL_LEAD_MS",
    "SECKILL_ACTIVE_WINDOW_MS",
    "SECKILL_FIXED_LEAD_MS",
    "SECKILL_CALIBRATION_INTERVAL_MS",
    "SECKILL_CALIBRATION_SAMPLE_SIZE",
    "SECKILL_RESPONSE_LOG_LIMIT",
    "SECKILL_RESPONSE_LOG_EVERY",
    "SECKILL_RESPONSE_LOG_BODY_CHARS",
    "SECKILL_GO_DISABLE_HTTP2",
}


def truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default


def mask_account(account: Any) -> str:
    value = str(account or "")
    if len(value) <= 4:
        return "*" * len(value)
    return value[:-4] + "****"


def now_text() -> str:
    return datetime.now(LOCAL_TZ).isoformat()


def today_time_after(minutes: float) -> str:
    return (datetime.now(LOCAL_TZ) + timedelta(minutes=max(0.0, minutes))).strftime("%H:%M:%S")


def selected_env(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    output: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if name not in ALLOWED_ENV_KEYS:
            continue
        if value is None or str(value).strip() == "":
            continue
        output[name] = str(value)
    return output


def build_failure_payload(request_payload: dict[str, Any], reason: str) -> dict[str, Any]:
    account_index = safe_int(request_payload.get("account_index"), 0)
    group_number = safe_int(request_payload.get("group_number") or request_payload.get("batch_number"), 10)
    group_name = str(request_payload.get("group_name") or f"seckill-batch{group_number}")
    masked = mask_account(request_payload.get("username"))
    row = {
        "account_index": account_index,
        "execution_order": safe_int(request_payload.get("execution_order"), account_index),
        "username": masked,
        "masked_username": masked,
        "group_name": group_name,
        "group_number": group_number,
        "group_position": f"{group_number}组账号{account_index}" if group_number else f"账号{account_index}",
        "sign_success": False,
        "sign_status": "FC执行失败",
        "has_reward": False,
        "password_error": False,
        "risk_controlled": False,
        "retry_count": 0,
        "is_final_retry": False,
        "detail_reason": reason[:800],
        "sign_time": "",
        "sign_ip": "",
        "sign_completed_at": now_text(),
        "activity_records": {
            "seckill": [
                {
                    "attempts_sent": 0,
                    "success": False,
                    "last_response_message": reason[:800],
                    "response_counts": {f"fc_error={reason[:160]}": 1},
                }
            ]
        },
    }
    return {
        "generated_at": now_text(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "total_accounts": 1,
        "results": [row],
    }


def sanitize_payload(payload: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    group_number = safe_int(request_payload.get("group_number") or request_payload.get("batch_number"), 10)
    group_name = str(request_payload.get("group_name") or payload.get("group_name") or f"seckill-batch{group_number}")
    payload["group_name"] = group_name
    payload["batch_name"] = group_name
    payload["group_number"] = group_number
    rows = payload.get("results")
    if not isinstance(rows, list):
        payload["results"] = []
        return payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        masked = str(row.get("masked_username") or mask_account(row.get("username") or request_payload.get("username")))
        row["username"] = masked
        row["masked_username"] = masked
        row.pop("password", None)
        row["group_name"] = group_name
        row["group_number"] = safe_int(row.get("group_number"), group_number)
        account_index = safe_int(row.get("account_index") or request_payload.get("account_index"), 0)
        row["account_index"] = account_index
        row["group_position"] = row.get("group_position") or f"{group_number}组账号{account_index}"
    return payload


def run_seckill(request_payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    username = str(request_payload.get("username") or "").strip()
    password = str(request_payload.get("password") or "").strip()
    if not username or not password:
        return 400, build_failure_payload(request_payload, "missing username/password")

    batch_number = safe_int(request_payload.get("batch_number"), 10)
    account_index = safe_int(request_payload.get("account_index"), 1)
    total_accounts = safe_int(request_payload.get("total_accounts"), 1)

    env = os.environ.copy()
    env.update(selected_env(request_payload.get("env")))
    env["TZ"] = "Asia/Shanghai"
    env["PYTHONUNBUFFERED"] = "1"
    env["SECKILL_SENDER"] = env.get("SECKILL_SENDER") or "go"
    env["GROUP_NUMBER"] = str(batch_number)
    env["GROUP_NAME"] = str(request_payload.get("group_name") or f"seckill-batch{batch_number}")
    env["ACCOUNT_INDEX"] = str(account_index)
    env["TOTAL_ACCOUNTS"] = str(total_accounts)
    env["RUNNER_TEMP"] = tempfile.gettempdir()
    env.setdefault("JLC_BIND_TOKEN_ENABLED", "true")

    if truthy(request_payload.get("relative_timing", True)):
        login_after = safe_float(request_payload.get("login_after_minutes"), 1.0)
        seckill_after = safe_float(request_payload.get("seckill_after_minutes"), 5.0)
        hard_stop_after = safe_float(request_payload.get("hard_stop_after_minutes"), 6.0)
        if login_after >= seckill_after:
            login_after = max(0.0, seckill_after - 1.0)
        if hard_stop_after <= seckill_after:
            hard_stop_after = seckill_after + 1.0
        env["SECKILL_LOGIN_AT"] = today_time_after(login_after)
        env["SECKILL_AT"] = today_time_after(seckill_after)
        env["SECKILL_HARD_STOP_TIME"] = today_time_after(hard_stop_after)

    target_keyword = str(request_payload.get("target_keyword") or "").strip()
    if target_keyword:
        env["SECKILL_TARGET_KEYWORD"] = target_keyword

    result_path = Path(tempfile.gettempdir()) / f"fc-seckill-result-{uuid.uuid4().hex}.json"
    env["RESULT_JSON_PATH"] = str(result_path)

    timeout_seconds = safe_int(request_payload.get("timeout_seconds"), 0)
    if timeout_seconds <= 0:
        timeout_seconds = max(600, int(safe_float(request_payload.get("hard_stop_after_minutes"), 6.0) * 60) + 300)

    cmd = [
        sys.executable,
        str(ROOT_DIR / "h3" / "seckill.py"),
        "--batch",
        str(batch_number),
        "--username",
        username,
        "--password",
        password,
        "--account-index",
        str(account_index),
        "--total-accounts",
        str(total_accounts),
    ]
    print(
        f"[fc] account={account_index}/{total_accounts} batch={batch_number} "
        f"user={mask_account(username)} timeout={timeout_seconds}s",
        flush=True,
    )
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT_DIR),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + "\n" + (exc.stderr or "")
        if output.strip():
            print(output[-12000:], flush=True)
        return 200, build_failure_payload(request_payload, f"FC subprocess timeout after {timeout_seconds}s")

    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    if output.strip():
        print(output[-20000:], flush=True)

    if not result_path.exists():
        return 200, build_failure_payload(
            request_payload,
            f"seckill.py did not create result json; exit={completed.returncode}",
        )
    try:
        with open(result_path, "r", encoding="utf-8") as file:
            result_payload = json.load(file)
    except Exception as exc:
        return 200, build_failure_payload(request_payload, f"cannot read result json: {type(exc).__name__}: {exc}")
    finally:
        try:
            result_path.unlink()
        except Exception:
            pass

    if not isinstance(result_payload, dict):
        return 200, build_failure_payload(request_payload, "result json is not an object")
    return 200, sanitize_payload(result_payload, request_payload)


class Handler(BaseHTTPRequestHandler):
    server_version = "jlc-seckill-fc/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def write_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/health"):
            self.write_json(200, {"ok": True, "time": now_text()})
            return
        self.write_json(404, {"ok": False, "message": "not found"})

    def do_POST(self) -> None:
        expected_token = os.getenv("FC_INVOKE_TOKEN", "").strip()
        auth_header = self.headers.get("Authorization", "")
        if not expected_token:
            self.write_json(500, {"ok": False, "message": "FC_INVOKE_TOKEN is not configured"})
            return
        if auth_header != f"Bearer {expected_token}":
            self.write_json(401, {"ok": False, "message": "unauthorized"})
            return

        try:
            length = safe_int(self.headers.get("Content-Length"), 0)
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be a json object")
        except Exception as exc:
            self.write_json(400, {"ok": False, "message": f"invalid json: {type(exc).__name__}: {exc}"})
            return

        status, result = run_seckill(payload)
        self.write_json(status, result)


def main() -> int:
    port = safe_int(os.getenv("FC_SERVER_PORT") or os.getenv("PORT"), 9000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[fc] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
