import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
WORKFLOWS = [
    {"workflow_file": f"seckill-batch{i}.yml", "artifact_name": f"seckill-batch{i}-result", "group_number": i}
    for i in range(1, 10)
]


def api_get(url: str, token: str, params=None):
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        params=params,
        timeout=40,
    )
    response.raise_for_status()
    return response.json()


def iso_to_local_date(text: str) -> str:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def target_date() -> str:
    hint = (os.getenv("TARGET_DATE") or os.getenv("TARGET_DATE_HINT") or "").strip()
    if hint:
        if "T" in hint:
            return iso_to_local_date(hint)
        return hint[:10]
    return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")


def pick_run(repo: str, token: str, workflow_file: str, date_text: str):
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/runs"
    payload = api_get(url, token, params={"status": "completed", "per_page": 30})
    for run in payload.get("workflow_runs", []):
        source_time = run.get("created_at") or run.get("run_started_at") or run.get("updated_at")
        if source_time and iso_to_local_date(source_time) == date_text:
            return run
    return None


def download_artifact(repo: str, token: str, run_id: int, artifact_name: str, target_dir: str):
    url = f"https://api.github.com/repos/{repo}/actions/runs/{run_id}/artifacts"
    payload = api_get(url, token, params={"per_page": 100})
    for artifact in payload.get("artifacts", []):
        if artifact.get("expired") or artifact.get("name") != artifact_name:
            continue
        response = requests.get(
            artifact["archive_download_url"],
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=60,
            allow_redirects=True,
        )
        response.raise_for_status()
        os.makedirs(target_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            archive.extractall(target_dir)
        return artifact
    return None


def main():
    output_dir = sys.argv[1] if len(sys.argv) > 1 else "results"
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY")
    if not token or not repo:
        print("missing GITHUB_TOKEN/GITHUB_REPOSITORY", flush=True)
        return 1

    os.makedirs(output_dir, exist_ok=True)
    date_text = target_date()
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": date_text,
        "batches": [],
    }
    found_any = False
    for item in WORKFLOWS:
        batch = dict(item)
        batch["found"] = False
        run = pick_run(repo, token, item["workflow_file"], date_text)
        if not run:
            batch["reason"] = "workflow run not found for target date"
            manifest["batches"].append(batch)
            continue
        batch["run_id"] = run.get("id")
        batch["run_url"] = run.get("html_url")
        batch["conclusion"] = run.get("conclusion")
        target_dir = os.path.join(output_dir, f"batch{item['group_number']}")
        artifact = download_artifact(repo, token, run["id"], item["artifact_name"], target_dir)
        if not artifact:
            batch["reason"] = "result artifact not found"
            manifest["batches"].append(batch)
            continue
        batch["found"] = True
        batch["artifact_id"] = artifact.get("id")
        batch["extract_dir"] = target_dir
        manifest["batches"].append(batch)
        found_any = True

    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if found_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
