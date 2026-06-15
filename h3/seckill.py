import argparse
import concurrent.futures
import importlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml
from dotenv import load_dotenv

try:
    import httpx
except Exception:
    httpx = None


ROOT_DIR = Path(__file__).resolve().parents[1]
H3_DIR = Path(__file__).resolve().parent
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
load_dotenv(ROOT_DIR / ".env")


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


def now_ms() -> float:
    return time.time() * 1000.0


def log(message: str):
    print(f"[{datetime.now(LOCAL_TZ).strftime('%H:%M:%S.%f')[:-3]}] {message}", flush=True)


def format_ms(timestamp_ms: float) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000.0, LOCAL_TZ).strftime("%H:%M:%S.%f")[:-3]


def mask_account(account: str) -> str:
    value = str(account or "")
    if len(value) <= 4:
        return "*" * len(value)
    return value[:-4] + "****"


def parse_iso_ms(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except Exception:
        return None


def today_at(time_text: str) -> datetime:
    hour, minute, second = [int(part) for part in str(time_text).split(":")]
    now = datetime.now(LOCAL_TZ)
    return now.replace(hour=hour, minute=minute, second=second, microsecond=0)


def epoch_ms(dt: datetime) -> float:
    return dt.timestamp() * 1000.0


def wait_until_ms(target_ms: float, label: str = ""):
    while True:
        remaining = target_ms - now_ms()
        if remaining <= 0:
            return
        if remaining > 60_000:
            if label:
                log(f"{label}: waiting {remaining / 1000:.0f}s")
            time.sleep(60)
        elif remaining > 1000:
            time.sleep(max(0.1, (remaining - 500) / 1000))
        else:
            time.sleep(max(0.001, remaining / 1000))


def load_config(path: str, batch_number: int) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    defaults = raw.get("defaults") or {}
    batches = raw.get("batches") or {}
    batch_key = f"batch{batch_number}"
    batch = batches.get(batch_key)
    if not isinstance(batch, dict):
        raise SystemExit(f"missing {batch_key} in {path}")
    config = dict(defaults)
    config.update(batch)
    config["batch_key"] = batch_key
    config["batch_number"] = batch_number
    return apply_env_overrides(config)


def apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    mappings = {
        "SECKILL_TARGET_KEYWORD": ("target_keyword", str),
        "SECKILL_BASE_URL": ("base_url", str),
        "SECKILL_CATEGORY_ACCESS_ID": ("category_access_id", str),
        "SECKILL_SOURCE": ("source", int),
        "SECKILL_LOGIN_AT": ("login_at", str),
        "SECKILL_AT": ("seckill_at", str),
        "SECKILL_HARD_STOP_TIME": ("hard_stop_time", str),
        "SECKILL_PREWARM_CONCURRENCY": ("prewarm_concurrency", int),
        "SECKILL_BURST_CONCURRENCY": ("burst_concurrency", int),
        "SECKILL_BURST_INTERVAL_MS": ("burst_interval_ms", int),
        "SECKILL_PREWARM_SERVER_ARRIVAL_LEAD_MS": ("prewarm_server_arrival_lead_ms", int),
        "SECKILL_ACTIVE_WINDOW_MS": ("active_window_ms", int),
        "SECKILL_FIXED_LEAD_MS": ("fixed_lead_ms", int),
        "SECKILL_CALIBRATION_INTERVAL_MS": ("calibration_interval_ms", int),
        "SECKILL_CALIBRATION_SAMPLE_SIZE": ("calibration_sample_size", int),
        "SECKILL_RESPONSE_LOG_LIMIT": ("response_log_limit", int),
        "SECKILL_RESPONSE_LOG_EVERY": ("response_log_every", int),
        "SECKILL_RESPONSE_LOG_BODY_CHARS": ("response_log_body_chars", int),
    }
    for env_name, (key, caster) in mappings.items():
        raw = os.getenv(env_name)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            config[key] = caster(raw)
        except Exception:
            config[key] = raw
    return config


def build_url(config: dict[str, Any], path_key: str) -> str:
    base_url = str(config.get("base_url") or os.getenv("SECKILL_BASE_URL") or os.getenv("BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("missing SECKILL_BASE_URL or BASE_URL")
    path = str(config.get(path_key) or "").strip()
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base_url + "/" + path.lstrip("/")


def choose_goods(rows: list[dict[str, Any]], keyword: str) -> dict[str, Any] | None:
    keyword = str(keyword or "").strip()
    if not keyword:
        return None
    for row in rows:
        title = str(row.get("skuTitle") or row.get("goodsName") or row.get("name") or "").strip()
        sku = str(row.get("skuCode") or "").strip()
        if keyword in title or keyword.lower() in sku.lower():
            return row
    return None


@dataclass
class CalibrationSample:
    server_delta_ms: float
    rtt_ms: float


@dataclass
class SeckillRuntime:
    config: dict[str, Any]
    target_goods: dict[str, Any] | None = None
    target_activity_start_ms: float | None = None
    samples: list[CalibrationSample] = field(default_factory=list)
    attempts_sent: int = 0
    first_response: dict[str, Any] | None = None
    success_response: dict[str, Any] | None = None
    success_sent_at: str = ""
    success_received_at: str = ""
    last_response_message: str = ""
    response_logs_emitted: int = 0
    response_counts: dict[str, int] = field(default_factory=dict)
    counter_lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def add_sample(self, sample: CalibrationSample):
        limit = safe_int(self.config.get("calibration_sample_size"), 15)
        self.samples.append(sample)
        if len(self.samples) > limit:
            self.samples = self.samples[-limit:]

    def median_delta(self) -> float | None:
        if not self.samples:
            return None
        return statistics.median(item.server_delta_ms for item in self.samples)

    def median_rtt(self) -> float | None:
        if not self.samples:
            return None
        return statistics.median(item.rtt_ms for item in self.samples)

    def resolved_start_ms(self) -> float:
        if self.target_activity_start_ms:
            return self.target_activity_start_ms
        return epoch_ms(today_at(str(self.config.get("seckill_at") or "10:00:00")))


def normalize_goods_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    body = data.get("data") if isinstance(data, dict) else None
    if isinstance(body, dict):
        rows = body.get("seckillGoodsResponseVos") or body.get("list") or body.get("records") or []
        return [row for row in rows if isinstance(row, dict)]
    if isinstance(body, list):
        return [row for row in body if isinstance(row, dict)]
    return []


def safe_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "msg", "errorMessage", "error"):
            value = data.get(key)
            if value:
                return str(value)
        try:
            return json.dumps(data, ensure_ascii=False)[:500]
        except Exception:
            return str(data)[:500]
    return str(data or "")[:500]


def json_preview(data: Any, fallback: str = "", limit: int = 500) -> str:
    try:
        if isinstance(data, dict):
            text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        elif data is not None:
            text = str(data)
        else:
            text = fallback or ""
    except Exception:
        text = fallback or str(data or "")
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(truncated,len={len(text)})"


def response_fingerprint(data: Any, http_status: int) -> str:
    if isinstance(data, dict):
        return (
            f"http={http_status} code={data.get('code')} "
            f"success={data.get('success')} msg={safe_message(data)[:160]}"
        )
    return f"http={http_status} non-json"


def go_sender_binary() -> Path | None:
    configured = (os.getenv("SECKILL_GO_SENDER") or "").strip()
    if configured:
        path = Path(configured)
        return path if path.exists() else None
    suffix = ".exe" if os.name == "nt" else ""
    for path in (H3_DIR / f"seckill_sender{suffix}", ROOT_DIR / f"seckill_sender{suffix}"):
        if path.exists():
            return path
    return None


def normalize_response_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, int] = {}
    for key, count in value.items():
        output[str(key)] = safe_int(count, 0)
    return output


def prepare_legacy_env(config: dict[str, Any]):
    base_url = str(config.get("base_url") or os.getenv("SECKILL_BASE_URL") or os.getenv("BASE_URL") or "").strip()
    referer = str(config.get("referer") or os.getenv("SECKILL_REFERER") or os.getenv("REFERER") or "").strip()
    if base_url and not os.getenv("BASE_URL"):
        os.environ["BASE_URL"] = base_url
    if referer:
        if not os.getenv("REFERER"):
            os.environ["REFERER"] = referer
        if not os.getenv("JLC_REFERER"):
            os.environ["JLC_REFERER"] = referer
    if os.getenv("SECKILL_CLIENT_TYPE"):
        os.environ.setdefault("JLC_CLIENT_TYPE", os.getenv("SECKILL_CLIENT_TYPE", ""))
    if os.getenv("SECKILL_USE_HTTP2"):
        os.environ.setdefault("JLC_USE_HTTP2", os.getenv("SECKILL_USE_HTTP2", ""))


def import_legacy_script(config: dict[str, Any]):
    prepare_legacy_env(config)
    if str(H3_DIR) not in sys.path:
        sys.path.insert(0, str(H3_DIR))
    return importlib.import_module("script")


def install_seckill_client(legacy, runtime: SeckillRuntime):
    class SeckillApiClient(legacy.ApiClient):
        def __init__(self, access_token, secretkey, account_index, page, user_agent=None):
            super().__init__(access_token, secretkey, account_index, page, user_agent=user_agent)
            self.base_url = str(
                runtime.config.get("base_url") or os.getenv("SECKILL_BASE_URL") or os.getenv("BASE_URL") or ""
            ).rstrip("/")
            if not self.base_url:
                raise RuntimeError("missing SECKILL_BASE_URL or BASE_URL")
            self.list_url = build_url(runtime.config, "list_path")
            self.buy_url = build_url(runtime.config, "buy_path")
            self.category_access_id = str(
                runtime.config.get("category_access_id") or os.getenv("SECKILL_CATEGORY_ACCESS_ID") or ""
            ).strip()
            if not self.category_access_id:
                raise RuntimeError("missing SECKILL_CATEGORY_ACCESS_ID")
            self.source = safe_int(runtime.config.get("source"), 4)
            self.seckill_record: dict[str, Any] = {}
            self.headers["origin"] = self.base_url
            self.headers["referer"] = str(runtime.config.get("referer") or os.getenv("SECKILL_REFERER") or "")
            self.headers.setdefault("sec-fetch-site", "same-origin")
            self.headers.setdefault("sec-fetch-mode", "cors")
            self.headers.setdefault("sec-fetch-dest", "empty")
            if httpx is not None and truthy(os.getenv("JLC_USE_HTTP2", "true")):
                try:
                    if self.http:
                        self.http.close()
                    self.http = httpx.Client(
                        http2=True,
                        timeout=5.0,
                        limits=httpx.Limits(max_connections=240, max_keepalive_connections=120),
                    )
                except Exception:
                    self.http = None

        def _post(self, url: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, float, float, str, int]:
            start = now_ms()
            response_text = ""
            status_code = 0
            try:
                if self.http:
                    response = self.http.post(url, headers=self.headers, json=payload)
                else:
                    response = requests.post(url, headers=self.headers, json=payload, timeout=5)
                end = now_ms()
                status_code = int(response.status_code)
                response_text = response.text
                try:
                    return response.json(), start, end, response_text, status_code
                except Exception:
                    return {"success": False, "code": status_code, "message": response_text[:500]}, start, end, response_text, status_code
            except Exception as exc:
                end = now_ms()
                return {"success": False, "code": 0, "message": f"{type(exc).__name__}: {exc}"}, start, end, response_text, status_code

        def fetch_goods_once(self, tag: str = "goods") -> dict[str, Any] | None:
            payload = {"categoryAccessId": self.category_access_id}
            data, local_start, local_end, _, _ = self._post(self.list_url, payload)
            if not isinstance(data, dict):
                return None
            body = data.get("data") if isinstance(data.get("data"), dict) else {}
            server_ms = parse_iso_ms(body.get("currentTime")) if isinstance(body, dict) else None
            if server_ms is not None:
                rtt = max(0.0, local_end - local_start)
                delta = server_ms - (local_end - rtt / 2.0)
                runtime.add_sample(CalibrationSample(server_delta_ms=delta, rtt_ms=rtt))
                log(
                    f"account {self.account_index} {tag}: rtt={rtt:.0f}ms "
                    f"delta={delta:.0f}ms samples={len(runtime.samples)}"
                )
            rows = normalize_goods_payload(data)
            target = choose_goods(rows, str(runtime.config.get("target_keyword") or ""))
            if target:
                runtime.target_goods = target
            start_ms = parse_iso_ms(body.get("activityBeginTime")) if isinstance(body, dict) else None
            if start_ms:
                runtime.target_activity_start_ms = start_ms
            return data

        def get_points(self):
            return 0

        def ensure_target_goods(self) -> bool:
            if runtime.target_goods:
                return True
            data = self.fetch_goods_once("target")
            if runtime.target_goods:
                goods = runtime.target_goods
                log(
                    f"account {self.account_index}: target matched "
                    f"{goods.get('skuTitle')} / {goods.get('skuCode')}"
                )
                return True
            self.sign_status = "秒杀商品未匹配"
            self.detail_reason = (
                f"未找到关键词 {runtime.config.get('target_keyword')} 对应商品；"
                f"列表响应: {safe_message(data)}"
            )
            return False

        def calibrate_until(self, deadline_ms: float):
            if str(runtime.config.get("schedule_mode")).lower() != "dynamic":
                return
            if not truthy(runtime.config.get("calibration_enabled", True)):
                return
            interval_ms = safe_int(runtime.config.get("calibration_interval_ms"), 1500)
            while now_ms() < deadline_ms and not runtime.stop_event.is_set():
                self.fetch_goods_once("calibrate")
                remaining = deadline_ms - now_ms()
                if remaining <= 0:
                    break
                time.sleep(min(interval_ms, max(50, remaining)) / 1000.0)

        def schedule_times(self) -> tuple[float, float, float, str]:
            start_server_ms = runtime.resolved_start_ms()
            active_end_server_ms = start_server_ms + safe_int(runtime.config.get("active_window_ms"), 3000)
            mode = str(runtime.config.get("schedule_mode") or "fixed").lower()
            if mode == "dynamic" and runtime.median_delta() is not None and runtime.median_rtt() is not None:
                delta = float(runtime.median_delta() or 0.0)
                rtt = float(runtime.median_rtt() or 0.0)
                arrival_lead = safe_int(runtime.config.get("prewarm_server_arrival_lead_ms"), 10)
                prewarm_ms = start_server_ms - delta - (rtt / 2.0) - arrival_lead
                formal_ms = start_server_ms - delta - (rtt / 2.0)
                active_end_ms = active_end_server_ms - delta - (rtt / 2.0)
                detail = f"dynamic delta={delta:.0f}ms rtt={rtt:.0f}ms arrival_lead={arrival_lead}ms"
                return prewarm_ms, formal_ms, active_end_ms, detail

            fixed_lead = safe_int(runtime.config.get("fixed_lead_ms"), 500)
            start_local_ms = epoch_ms(today_at(str(runtime.config.get("seckill_at") or "10:00:00")))
            return (
                start_local_ms - fixed_lead,
                start_local_ms,
                start_local_ms + safe_int(runtime.config.get("active_window_ms"), 3000),
                f"fixed lead={fixed_lead}ms",
            )

        def send_exchange_once(self) -> dict[str, Any]:
            goods = runtime.target_goods or {}
            detail_id = str(goods.get("voucherSeckillActivityDetailAccessId") or goods.get("goodsDetailAccessId") or "").strip()
            payload = {
                "goodsDetailAccessId": detail_id,
                "categoryAccessId": self.category_access_id,
                "source": self.source,
            }
            with runtime.counter_lock:
                runtime.attempts_sent += 1
                attempt_number = runtime.attempts_sent
            data, local_start, local_end, text, http_status = self._post(self.buy_url, payload)
            sent_at = datetime.fromtimestamp(local_start / 1000.0, LOCAL_TZ)
            received_at = datetime.fromtimestamp(local_end / 1000.0, LOCAL_TZ)
            success = isinstance(data, dict) and data.get("code") == 200 and data.get("success") is True
            fingerprint = response_fingerprint(data, http_status)
            should_log = success
            with runtime.counter_lock:
                runtime.response_counts[fingerprint] = runtime.response_counts.get(fingerprint, 0) + 1
                if runtime.first_response is None:
                    runtime.first_response = data if isinstance(data, dict) else {"raw": text[:500]}
                    should_log = True
                if isinstance(data, dict):
                    runtime.last_response_message = safe_message(data)
                log_limit = max(0, safe_int(runtime.config.get("response_log_limit"), 50))
                log_every = max(0, safe_int(runtime.config.get("response_log_every"), 200))
                if runtime.response_logs_emitted < log_limit:
                    should_log = True
                if log_every and attempt_number % log_every == 0:
                    should_log = True
                if should_log:
                    runtime.response_logs_emitted += 1
                if success:
                    runtime.success_response = data
                    runtime.success_sent_at = sent_at.isoformat()
                    runtime.success_received_at = received_at.isoformat()
                    runtime.stop_event.set()
            if should_log:
                preview_limit = safe_int(runtime.config.get("response_log_body_chars"), 500)
                log(
                    f"account {self.account_index}: response #{attempt_number} "
                    f"sent={format_ms(local_start)} recv={format_ms(local_end)} "
                    f"rtt={local_end - local_start:.1f}ms http={http_status} "
                    f"body={json_preview(data, text, preview_limit)}"
                )
            return data if isinstance(data, dict) else {"success": False, "code": 0, "message": text[:500]}

        def fire_concurrent(self, count: int, executor: concurrent.futures.ThreadPoolExecutor) -> list[concurrent.futures.Future]:
            if count <= 0 or runtime.stop_event.is_set():
                return []
            return [executor.submit(self.send_exchange_once) for _ in range(count)]

        def run_go_seckill_window(self, prewarm_ms: float, formal_ms: float, active_end_ms: float, hard_stop_ms: float) -> bool:
            mode = str(os.getenv("SECKILL_SENDER", "go") or "go").strip().lower()
            if mode in {"python", "py"}:
                log(f"account {self.account_index}: SECKILL_SENDER=python, using Python sender")
                return False

            binary = go_sender_binary()
            if not binary:
                log(f"account {self.account_index}: Go sender binary not found, using Python sender fallback")
                return False

            goods = runtime.target_goods or {}
            detail_id = str(goods.get("voucherSeckillActivityDetailAccessId") or goods.get("goodsDetailAccessId") or "").strip()
            payload = {
                "goodsDetailAccessId": detail_id,
                "categoryAccessId": self.category_access_id,
                "source": self.source,
            }
            config_payload = {
                "account_index": self.account_index,
                "buy_url": self.buy_url,
                "headers": dict(self.headers),
                "payload": payload,
                "prewarm_ms": int(round(prewarm_ms)),
                "formal_ms": int(round(formal_ms)),
                "active_end_ms": int(round(active_end_ms)),
                "hard_stop_ms": int(round(hard_stop_ms)),
                "prewarm_concurrency": safe_int(runtime.config.get("prewarm_concurrency"), 30),
                "burst_concurrency": safe_int(runtime.config.get("burst_concurrency"), 120),
                "burst_interval_ms": safe_int(runtime.config.get("burst_interval_ms"), 10),
                "response_log_limit": safe_int(runtime.config.get("response_log_limit"), 50),
                "response_log_every": safe_int(runtime.config.get("response_log_every"), 200),
                "response_log_body_chars": safe_int(runtime.config.get("response_log_body_chars"), 500),
                "disable_http2": truthy(os.getenv("SECKILL_GO_DISABLE_HTTP2", "false")),
            }

            temp_dir = Path(os.getenv("RUNNER_TEMP") or tempfile.gettempdir())
            temp_dir.mkdir(parents=True, exist_ok=True)
            config_path = temp_dir / f"seckill-sender-{os.getpid()}-{self.account_index}.json"
            output_path = temp_dir / f"seckill-sender-result-{os.getpid()}-{self.account_index}.json"
            try:
                with open(config_path, "w", encoding="utf-8") as file:
                    json.dump(config_payload, file, ensure_ascii=False)
                timeout_seconds = max(60, int(max(0, hard_stop_ms - now_ms()) / 1000) + 20)
                log(
                    f"account {self.account_index}: using Go sender "
                    f"binary={binary} timeout={timeout_seconds}s"
                )
                completed = subprocess.run(
                    [str(binary), "-config", str(config_path), "-output", str(output_path)],
                    cwd=str(ROOT_DIR),
                    timeout=timeout_seconds,
                )
                if completed.returncode != 0:
                    log(f"account {self.account_index}: Go sender exited {completed.returncode}, using Python sender fallback")
                    return False
                if not output_path.exists():
                    log(f"account {self.account_index}: Go sender result missing, using Python sender fallback")
                    return False
                with open(output_path, "r", encoding="utf-8") as file:
                    result_payload = json.load(file)
            except Exception as exc:
                log(f"account {self.account_index}: Go sender failed: {type(exc).__name__}: {exc}; using Python sender fallback")
                return False
            finally:
                for path in (config_path, output_path):
                    try:
                        path.unlink()
                    except Exception:
                        pass

            runtime.attempts_sent = safe_int(result_payload.get("attempts_sent"), 0)
            runtime.first_response = result_payload.get("first_response") if result_payload.get("first_response") is not None else {}
            runtime.response_counts = normalize_response_counts(result_payload.get("response_counts"))
            runtime.last_response_message = str(result_payload.get("last_response_message") or "")
            if truthy(result_payload.get("success")):
                runtime.success_response = result_payload.get("success_response") if result_payload.get("success_response") is not None else {}
                runtime.success_sent_at = str(result_payload.get("success_sent_at") or "")
                runtime.success_received_at = str(result_payload.get("success_received_at") or "")
                runtime.stop_event.set()
            else:
                runtime.success_response = None
            log(
                f"account {self.account_index}: Go sender finished attempts={runtime.attempts_sent} "
                f"success={bool(runtime.success_response)}"
            )
            return True

        def run_seckill_window(self):
            prewarm_count = safe_int(runtime.config.get("prewarm_concurrency"), 30)
            burst_count = safe_int(runtime.config.get("burst_concurrency"), 120)
            interval_ms = safe_int(runtime.config.get("burst_interval_ms"), 10)
            hard_stop_ms = epoch_ms(today_at(str(runtime.config.get("hard_stop_time") or "10:01:00")))

            prewarm_ms, formal_ms, active_end_ms, detail = self.schedule_times()
            calibration_deadline = min(prewarm_ms - 100, hard_stop_ms)
            self.calibrate_until(calibration_deadline)
            prewarm_ms, formal_ms, active_end_ms, detail = self.schedule_times()
            log(
                f"account {self.account_index}: schedule {detail}; "
                f"prewarm={datetime.fromtimestamp(prewarm_ms / 1000, LOCAL_TZ).strftime('%H:%M:%S.%f')[:-3]} "
                f"formal={datetime.fromtimestamp(formal_ms / 1000, LOCAL_TZ).strftime('%H:%M:%S.%f')[:-3]}"
            )

            if self.run_go_seckill_window(prewarm_ms, formal_ms, active_end_ms, hard_stop_ms):
                return

            max_workers = max(prewarm_count, burst_count, 1)
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            try:
                wait_until_ms(prewarm_ms, "prewarm")
                if now_ms() < hard_stop_ms and not runtime.stop_event.is_set():
                    log(f"account {self.account_index}: prewarm {prewarm_count} concurrent requests")
                    self.fire_concurrent(prewarm_count, executor)

                wait_until_ms(formal_ms, "formal")
                next_round = formal_ms
                round_number = 0
                while now_ms() <= active_end_ms and now_ms() < hard_stop_ms and not runtime.stop_event.is_set():
                    wait_until_ms(next_round)
                    if runtime.stop_event.is_set() or now_ms() >= hard_stop_ms:
                        break
                    round_number += 1
                    self.fire_concurrent(burst_count, executor)
                    next_round += interval_ms
                log(
                    f"account {self.account_index}: send loop ended rounds={round_number} "
                    f"attempts={runtime.attempts_sent} success={bool(runtime.success_response)}"
                )
                if runtime.response_counts:
                    summary = sorted(runtime.response_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
                    log(
                        f"account {self.account_index}: response summary "
                        + " | ".join(f"{count}x {key}" for key, count in summary)
                    )

                drain_until = min(hard_stop_ms, now_ms() + 3000)
                while now_ms() < drain_until and not runtime.stop_event.is_set():
                    time.sleep(0.05)
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

        def build_record(self) -> dict[str, Any]:
            goods = runtime.target_goods or {}
            success = runtime.success_response is not None
            return {
                "title": str(goods.get("skuTitle") or "").strip(),
                "sku_code": str(goods.get("skuCode") or "").strip(),
                "goods_detail_access_id": str(
                    goods.get("voucherSeckillActivityDetailAccessId") or goods.get("goodsDetailAccessId") or ""
                ).strip(),
                "category_access_id": self.category_access_id,
                "target_keyword": str(runtime.config.get("target_keyword") or "").strip(),
                "schedule_mode": str(runtime.config.get("schedule_mode") or "").strip(),
                "attempts_sent": runtime.attempts_sent,
                "success": success,
                "success_sent_at": runtime.success_sent_at,
                "success_received_at": runtime.success_received_at,
                "first_response": runtime.first_response or {},
                "success_response": runtime.success_response or {},
                "last_response_message": runtime.last_response_message,
                "response_counts": runtime.response_counts,
                "calibration_samples": len(runtime.samples),
                "median_server_delta_ms": runtime.median_delta(),
                "median_rtt_ms": runtime.median_rtt(),
            }

        def execute_full_process(self):
            self.initial_points = 0
            self.final_points = 0
            if not self.ensure_target_goods():
                self.seckill_record = self.build_record()
                self.activity_records = {"seckill": [self.seckill_record], "lottery": []}
                return False
            self.run_seckill_window()
            self.seckill_record = self.build_record()
            self.activity_records = {"seckill": [self.seckill_record], "lottery": []}
            self.has_reward = bool(runtime.success_response)
            self.sign_completed_at = datetime.now(LOCAL_TZ).isoformat()
            if runtime.success_response:
                self.sign_status = "秒杀成功"
                self.detail_reason = f"抢到 {self.seckill_record.get('title')}"
                return True
            self.sign_status = "秒杀失败"
            self.detail_reason = runtime.last_response_message or "未在秒杀窗口内收到成功响应"
            return False

        def fetch_activity_records(self) -> dict[str, Any]:
            if not self.seckill_record:
                self.seckill_record = self.build_record()
            self.activity_records = {"seckill": [self.seckill_record], "lottery": []}
            return self.activity_records

    legacy.ApiClient = SeckillApiClient


def build_output_payload(result: dict[str, Any], runtime: SeckillRuntime, username: str, account_index: int) -> dict[str, Any]:
    group_number = safe_int(runtime.config.get("group_number"), safe_int(runtime.config.get("batch_number"), 0))
    group_name = str(runtime.config.get("group_name") or f"seckill-batch{group_number}")
    result.setdefault("username", username)
    result.setdefault("masked_username", mask_account(username))
    result.setdefault("account_index", account_index)
    result["group_name"] = group_name
    result["group_number"] = group_number
    result["group_position"] = f"{group_number}组账号{account_index}" if group_number else f"账号{account_index}"
    result["activity_records"] = result.get("activity_records") or {"seckill": []}
    payload = {
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "batch_name": group_name,
        "group_name": group_name,
        "group_number": group_number,
        "total_accounts": 1,
        "results": [result],
    }
    return payload


def write_result(path: str, payload: dict[str, Any]):
    output = Path(path)
    if output.parent and str(output.parent) != ".":
        output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)


def run_live(args) -> int:
    config = load_config(args.config, args.batch)
    login_target = today_at(str(config.get("login_at") or "09:50:00"))
    if datetime.now(LOCAL_TZ) < login_target:
        log(f"waiting until login time {login_target.strftime('%H:%M:%S')}")
        wait_until_ms(epoch_ms(login_target), "login")

    runtime = SeckillRuntime(config=config)
    legacy = import_legacy_script(config)
    install_seckill_client(legacy, runtime)

    result = None
    max_login_retries = max(0, safe_int(os.getenv("LOGIN_MAX_RETRIES"), 3))
    for attempt in range(max_login_retries + 1):
        result = legacy.sign_in_account(
            args.username,
            args.password,
            args.account_index,
            args.total_accounts,
            retry_count=attempt,
            is_final_retry=attempt >= max_login_retries,
        )
        if result.get("password_error"):
            break
        if result.get("token_extracted"):
            break
        if attempt >= max_login_retries:
            break
        status = str(result.get("sign_status") or "")
        reason = str(result.get("detail_reason") or "")
        log(
            f"account {args.account_index}: login/token not ready, retry "
            f"{attempt + 1}/{max_login_retries}; status={status}; reason={reason[:160]}"
        )
        time.sleep(random.uniform(3, 7))

    result = result or {}
    payload = build_output_payload(result, runtime, args.username, args.account_index)
    write_result(os.getenv("RESULT_JSON_PATH", "result.json"), payload)
    if truthy(os.getenv("SECKILL_EXIT_ON_FAILURE", "false")) and not result.get("sign_success"):
        return 1
    return 0


def run_self_test() -> int:
    sample = {
        "data": {
            "currentTime": "2026-06-15T01:59:58.000Z",
            "activityBeginTime": "2026-06-15T02:00:00.000Z",
            "seckillGoodsResponseVos": [
                {"skuCode": "SKUJX7", "skuTitle": "京东京造计数握力器 5-60KG", "voucherSeckillActivityDetailAccessId": "a"},
                {"skuCode": "QBVO", "skuTitle": "铝合金板类专属 9 折券", "voucherSeckillActivityDetailAccessId": "b"},
            ],
        }
    }
    rows = normalize_goods_payload(sample)
    assert choose_goods(rows, "握力器")["voucherSeckillActivityDetailAccessId"] == "a"
    assert choose_goods(rows, "铝合金")["voucherSeckillActivityDetailAccessId"] == "b"
    runtime = SeckillRuntime(config={"calibration_sample_size": 3})
    runtime.add_sample(CalibrationSample(server_delta_ms=100, rtt_ms=40))
    runtime.add_sample(CalibrationSample(server_delta_ms=120, rtt_ms=60))
    runtime.add_sample(CalibrationSample(server_delta_ms=110, rtt_ms=50))
    assert runtime.median_delta() == 110
    assert runtime.median_rtt() == 50
    print("seckill self-test ok")
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description="JLC seckill runner")
    parser.add_argument("--batch", type=int, default=safe_int(os.getenv("GROUP_NUMBER"), 1))
    parser.add_argument("--config", default=str(H3_DIR / "seckill_config.yml"))
    parser.add_argument("--username", default=os.getenv("ACCOUNT_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("ACCOUNT_PASSWORD", ""))
    parser.add_argument("--account-index", type=int, default=safe_int(os.getenv("ACCOUNT_INDEX"), 1))
    parser.add_argument("--total-accounts", type=int, default=safe_int(os.getenv("TOTAL_ACCOUNTS"), 20))
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.self_test:
        return run_self_test()
    if not args.username or not args.password:
        raise SystemExit("missing --username/--password or ACCOUNT_USERNAME/ACCOUNT_PASSWORD")
    return run_live(args)


if __name__ == "__main__":
    raise SystemExit(main())
