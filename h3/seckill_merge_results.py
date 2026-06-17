import glob
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def truthy(value) -> bool:
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default


def load_result(path: str):
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
    except Exception:
        return None
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    row = dict(rows[0])
    row["_payload_group_name"] = payload.get("group_name") or payload.get("batch_name") or ""
    row["_payload_group_number"] = payload.get("group_number") or 0
    return row


def score(row: dict):
    return (
        1 if truthy(row.get("sign_success")) else 0,
        safe_int(row.get("retry_count"), 0),
        1 if row.get("activity_records") else 0,
    )


PRESERVED_RESULT_FIELDS = (
    "runner_diagnostics",
    "login_started_at",
    "login_attempts",
    "token_extracted",
    "secretkey_extracted",
    "m_site_token_bound",
    "cookie_count",
    "cookie_attached_to_api",
)


def main():
    results_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "merged/result.json"
    rows_by_account = {}
    for path in glob.glob(os.path.join(results_dir, "**", "result.json"), recursive=True):
        row = load_result(path)
        if not row:
            continue
        account_index = safe_int(row.get("account_index"), 0)
        if account_index <= 0:
            continue
        current = rows_by_account.get(account_index)
        if current is None or score(row) >= score(current):
            rows_by_account[account_index] = row

    rows = [rows_by_account[key] for key in sorted(rows_by_account)]
    group_number = 0
    group_name = ""
    if rows:
        group_number = safe_int(rows[0].get("group_number") or rows[0].get("_payload_group_number"), 0)
        group_name = str(rows[0].get("group_name") or rows[0].get("_payload_group_name") or f"seckill-batch{group_number}")

    normalized = []
    for row in rows:
        account_index = safe_int(row.get("account_index"), 0)
        normalized_row = {
            "account_index": account_index,
            "execution_order": safe_int(row.get("execution_order"), account_index),
            "username": row.get("masked_username") or row.get("username") or f"账号{account_index}",
            "group_name": row.get("group_name") or group_name,
            "group_number": safe_int(row.get("group_number"), group_number),
            "group_position": row.get("group_position") or f"{group_number}组账号{account_index}",
            "sign_success": truthy(row.get("sign_success")),
            "sign_status": row.get("sign_status") or "",
            "has_reward": truthy(row.get("has_reward")),
            "password_error": truthy(row.get("password_error")),
            "risk_controlled": truthy(row.get("risk_controlled")),
            "retry_count": safe_int(row.get("retry_count"), 0),
            "is_final_retry": truthy(row.get("is_final_retry")),
            "detail_reason": row.get("detail_reason") or "",
            "sign_time": row.get("sign_time") or "",
            "sign_ip": row.get("sign_ip") or "",
            "sign_completed_at": row.get("sign_completed_at") or "",
            "activity_records": row.get("activity_records") or {"seckill": []},
        }
        for field_name in PRESERVED_RESULT_FIELDS:
            if field_name in row:
                normalized_row[field_name] = row[field_name]
        normalized.append(normalized_row)

    payload = {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "total_accounts": len(normalized),
        "results": normalized,
    }
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    print(json.dumps({"merged": len(normalized), "output": output_path}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
