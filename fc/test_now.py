import concurrent.futures
import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
OUTPUT_DIR = Path(os.getenv("FC_RESULTS_DIR") or "fc-results")
OUTPUT_PATH = OUTPUT_DIR / "result.json"

SECRET_ENV_KEYS = [
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
    "SECKILL_RESPONSE_LOG_LIMIT",
    "SECKILL_RESPONSE_LOG_EVERY",
    "SECKILL_RESPONSE_LOG_BODY_CHARS",
    "SECKILL_GO_DISABLE_HTTP2",
]


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


def truthy(value: Any) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def mask_account(account: Any) -> str:
    value = str(account or "")
    if len(value) <= 4:
        return "*" * len(value)
    return value[:-4] + "****"


def parse_accounts(raw: str) -> list[tuple[str, str]]:
    accounts: list[tuple[str, str]] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        if "----" in text:
            username, password = text.split("----", 1)
        elif "," in text:
            username, password = text.split(",", 1)
        else:
            print(f"[accounts] skipped invalid line: {text[:12]}***", flush=True)
            continue
        username = username.strip()
        password = password.strip()
        if username and password:
            accounts.append((username, password))
    return accounts


def selected_secret_env() -> dict[str, str]:
    payload: dict[str, str] = {}
    for name in SECRET_ENV_KEYS:
        value = os.getenv(name)
        if value is not None and str(value).strip():
            payload[name] = str(value)
    return payload


def failure_row(account_index: int, username: str, reason: str, group_number: int, group_name: str) -> dict[str, Any]:
    masked = mask_account(username)
    return {
        "account_index": account_index,
        "execution_order": account_index,
        "username": masked,
        "masked_username": masked,
        "group_name": group_name,
        "group_number": group_number,
        "group_position": f"{group_number}组账号{account_index}",
        "sign_success": False,
        "sign_status": "FC调用失败",
        "has_reward": False,
        "password_error": False,
        "risk_controlled": False,
        "retry_count": 0,
        "is_final_retry": False,
        "detail_reason": reason[:800],
        "sign_time": "",
        "sign_ip": "",
        "sign_completed_at": datetime.now(LOCAL_TZ).isoformat(),
        "activity_records": {
            "seckill": [
                {
                    "attempts_sent": 0,
                    "success": False,
                    "last_response_message": reason[:800],
                    "response_counts": {f"fc_call_error={reason[:160]}": 1},
                }
            ]
        },
    }


def sanitize_rows(rows: list[dict[str, Any]], fallback_user: str) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        masked = str(item.get("masked_username") or mask_account(item.get("username") or fallback_user))
        item["username"] = masked
        item["masked_username"] = masked
        item.pop("password", None)
        cleaned.append(item)
    return cleaned


def normalize_fc_response(data: Any, fallback_user: str) -> list[dict[str, Any]] | None:
    if isinstance(data, dict):
        rows = data.get("results")
        if isinstance(rows, list):
            return sanitize_rows([row for row in rows if isinstance(row, dict)], fallback_user)
        nested = data.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("results"), list):
            return sanitize_rows([row for row in nested["results"] if isinstance(row, dict)], fallback_user)
    return None


def invoke_fc(url: str, token: str, request_payload: dict[str, Any], timeout_seconds: int) -> list[dict[str, Any]]:
    account_index = safe_int(request_payload.get("account_index"), 0)
    username = str(request_payload.get("username") or "")
    group_number = safe_int(request_payload.get("group_number") or request_payload.get("batch_number"), 10)
    group_name = str(request_payload.get("group_name") or f"seckill-batch{group_number}")
    raw = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "github-actions-fc-seckill-test/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
            text = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(text)
            except Exception as exc:
                return [failure_row(account_index, username, f"FC returned non-json: {type(exc).__name__}: {text[:300]}", group_number, group_name)]
            rows = normalize_fc_response(data, username)
            if rows is None:
                return [failure_row(account_index, username, f"FC returned unexpected payload: {text[:500]}", group_number, group_name)]
            return rows
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return [failure_row(account_index, username, f"HTTP {exc.code}: {body[:500]}", group_number, group_name)]
    except Exception as exc:
        return [failure_row(account_index, username, f"{type(exc).__name__}: {exc}", group_number, group_name)]


def write_payload(rows: list[dict[str, Any]], group_number: int, group_name: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "total_accounts": len(rows),
        "results": rows,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    manifest = {
        "target_date": datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"),
        "mode": "fc-test-now",
        "total_accounts": len(rows),
    }
    with open(OUTPUT_DIR / "manifest.json", "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
    print(f"[result] wrote {OUTPUT_PATH} rows={len(rows)}", flush=True)


def main() -> int:
    url = (os.getenv("ALIYUN_FC_SECKILL_URL") or "").strip()
    token = (os.getenv("ALIYUN_FC_INVOKE_TOKEN") or "").strip()
    group_number = safe_int(os.getenv("FC_TEST_GROUP_NUMBER"), 16)
    group_name = os.getenv("FC_TEST_GROUP_NAME") or "seckill-fc-test"
    accounts = parse_accounts(os.getenv("ACCOUNTS_TEST") or "")
    limit = safe_int(os.getenv("FC_TEST_ACCOUNT_LIMIT"), 2)
    if limit > 0:
        accounts = accounts[:limit]

    if not accounts:
        print("[accounts] ACCOUNTS_TEST is empty or invalid", flush=True)
        write_payload([], group_number, group_name)
        return 0

    if not url or not token:
        reason = "ALIYUN_FC_SECKILL_URL or ALIYUN_FC_INVOKE_TOKEN is empty"
        rows = [failure_row(index, username, reason, group_number, group_name) for index, (username, _) in enumerate(accounts, 1)]
        write_payload(rows, group_number, group_name)
        return 0

    hard_stop_after = safe_float(os.getenv("HARD_STOP_AFTER_MINUTES"), 6.0)
    timeout_extra_seconds = safe_int(os.getenv("FC_CALL_TIMEOUT_EXTRA_SECONDS"), 180)
    timeout_seconds = max(180, int(hard_stop_after * 60) + max(60, timeout_extra_seconds))
    max_parallel = safe_int(os.getenv("FC_TEST_MAX_PARALLEL"), len(accounts))
    max_parallel = max(1, min(max_parallel, len(accounts)))
    secret_env = selected_secret_env()

    print(f"[fc-test] accounts={len(accounts)} parallel={max_parallel} timeout={timeout_seconds}s", flush=True)
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_parallel) as executor:
        futures = []
        for account_index, (username, password) in enumerate(accounts, 1):
            payload = {
                "batch_number": group_number,
                "group_number": group_number,
                "group_name": group_name,
                "account_index": account_index,
                "execution_order": account_index,
                "total_accounts": len(accounts),
                "username": username,
                "password": password,
                "relative_timing": truthy(os.getenv("RELATIVE_TIMING", "true")),
                "login_after_minutes": os.getenv("LOGIN_AFTER_MINUTES", "1"),
                "seckill_after_minutes": os.getenv("SECKILL_AFTER_MINUTES", "5"),
                "hard_stop_after_minutes": os.getenv("HARD_STOP_AFTER_MINUTES", "6"),
                "target_keyword": os.getenv("SECKILL_TARGET_KEYWORD", ""),
                "timeout_seconds": timeout_seconds,
                "env": secret_env,
            }
            print(f"[fc-test] submit account {account_index}/{len(accounts)} user={mask_account(username)}", flush=True)
            futures.append(executor.submit(invoke_fc, url, token, payload, timeout_seconds))

        for future in concurrent.futures.as_completed(futures):
            rows.extend(future.result())

    rows.sort(key=lambda item: safe_int(item.get("account_index"), 999))
    write_payload(rows, group_number, group_name)
    success_count = sum(1 for row in rows if truthy(row.get("sign_success")))
    print(f"[fc-test] done success={success_count} failed={len(rows) - success_count}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
