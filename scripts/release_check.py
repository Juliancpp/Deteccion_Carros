#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT_ROOT / "venv" / "bin" / "python"
sys.path.insert(0, str(PROJECT_ROOT))


def run(cmd: list[str], *, optional: bool = False) -> tuple[bool, str]:
    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return result.returncode == 0 or optional, result.stdout.strip()
    except Exception as exc:
        return optional, str(exc)


def git_ignored(path: str) -> bool:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=PROJECT_ROOT)
    return result.returncode == 0


def file_contains(path: str, text: str) -> bool:
    target = PROJECT_ROOT / path
    return target.exists() and text in target.read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline release-candidate validation.")
    parser.add_argument("--skip-real-cameras", action="store_true")
    parser.add_argument("--skip-soak-test", action="store_true")
    parser.add_argument("--json-output", action="store_true")
    args = parser.parse_args()

    checks: dict[str, dict[str, object]] = {}

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail[:500]}

    add("python_available", PYTHON.exists(), str(PYTHON))
    add("python_version_runtime", sys.version_info >= (3, 12), f"{sys.version_info.major}.{sys.version_info.minor}")
    ok, out = run([str(PYTHON), "-m", "pip", "check"])
    add("pip_check", ok, out)
    ok, out = run([str(PYTHON), "-m", "pytest", "-q"])
    add("pytest", ok, out.splitlines()[-1] if out else "")
    for rel in [
        "scripts/run_hotel_mvp.py",
        "scripts/soak_test_offline.py",
        "scripts/smoke_test_dashboard.py",
        "scripts/check_portable_paths.py",
        "scripts/install_production.sh",
        "app/api.py",
        "app/multicamera/event_consumer.py",
    ]:
        add(f"critical_file:{rel}", (PROJECT_ROOT / rel).exists())
    ok, out = run([str(PYTHON), "scripts/check_portable_paths.py"])
    add("portable_paths", ok, out)
    add("env_present", (PROJECT_ROOT / ".env").exists(), ".env is required on production host")
    add("env_ignored", git_ignored(".env"))
    add("model_present", (PROJECT_ROOT / "models" / "yolo11n.pt").exists(), "models/yolo11n.pt")
    add("model_ignored", git_ignored("models/yolo11n.pt"))
    add("sqlite_ignored", git_ignored("database/hotel_mvp.db"))
    add("media_ignored", git_ignored("data/captures/sample.jpg") and git_ignored("data/clips/sample.mp4"))
    import app.config as config
    add("hotel_event_source", config.HOTEL_EVENT_SOURCE == "phase1_analyzer")
    add("snapshots_atomic", file_contains("scripts/run_hotel_mvp.py", "atomic_write_jpeg") and file_contains("app/runtime.py", "cv2.imencode"))
    api_text = (PROJECT_ROOT / "app" / "api.py").read_text(encoding="utf-8")
    add("api_no_yolo_import", "VehicleDetector" not in api_text and "ultralytics" not in api_text)
    add("systemd_restart", file_contains("deploy/systemd/deteccion-carros.service", "Restart=always") and file_contains("deploy/systemd/deteccion-dashboard.service", "Restart=always"))
    ok, out = run(["bash", "-n", "scripts/install_production.sh"])
    add("installer_bash", ok, out)
    ok, out = run(["bash", "-n", "scripts/build_private_deploy_bundle.sh"])
    add("bundle_bash", ok, out)
    ok, out = run([str(PYTHON), "scripts/smoke_test_dashboard.py"])
    add("dashboard_smoke", ok, out)
    if args.skip_soak_test:
        add("offline_soak", True, "skipped")
    else:
        ok, out = run([str(PYTHON), "scripts/soak_test_offline.py", "--events", "1000", "--inject-failures"])
        add("offline_soak", ok, out)
    if args.skip_real_cameras:
        add("real_cameras", True, "skipped by request")
    else:
        add("real_cameras", False, "real DVR validation not available in this environment")

    overall = all(item["ok"] for item in checks.values())
    payload = {"ok": overall, "checks": checks}
    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for name, item in checks.items():
            print(f"{'OK' if item['ok'] else 'FAIL'} {name}: {item['detail']}")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
