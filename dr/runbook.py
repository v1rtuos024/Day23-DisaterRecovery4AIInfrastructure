"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
CONFIRM_INTERVAL = 5.0
P95_LIMIT_MS = 1000.0


def step(n, name, **kw):
    """Append one timestamped checklist event."""
    ts = time.time()
    event = {**kw, "ts": ts,
             "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
             "step": n, "name": name}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
        log.flush()
    print(line, flush=True)
    return event


def confirm(auto: bool, msg: str) -> bool:
    """Require explicit operator consent unless running a CI drill."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except (EOFError, KeyboardInterrupt):
        return False


def outage_event(primary: str) -> dict | None:
    """Use the latest unmatched chaos kill; never fabricate an outage time."""
    path = pathlib.Path("chaos/chaos-events.jsonl")
    if not path.exists():
        return None
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    for event in reversed(events):
        if event.get("region") != primary:
            continue
        if event.get("action") == "restore":
            return None
        if event.get("action") == "kill":
            return event
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute one operator-approved failover, then verify its results."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions a/b")
    if backend not in {"fs", "minio"}:
        raise ValueError("invalid snapshot backend")
    started = time.monotonic()
    result = {"ok": False, "primary": primary, "target": target, "cutover": False}
    current_step = 1
    names = {1: "xac_nhan_outage", 2: "thong_bao_incident",
             3: "scale_gpu_pool", 4: "verify_state_replica",
             5: "dns_cutover", 6: "verify_golden_signals"}
    try:
        observations = []
        for attempt in range(3):
            probes = {}
            for region in (primary, target):
                ready, reason = hc.probe(region, 2.0)
                probes[region] = {"ready": ready, "reason": reason}
            observations.append({"ts": time.time(), "regions": probes})
            if attempt < 2:
                time.sleep(CONFIRM_INTERVAL)
        confirmed = all(not item["regions"][primary]["ready"] for item in observations)
        step(1, names[1], ok=confirmed, observations=observations,
             interval_s=CONFIRM_INTERVAL, threshold=3)
        if not confirmed:
            result["reason"] = "primary did not fail three consecutive readiness probes"
            return result

        current_step = 2
        outage = outage_event(primary)
        t_outage = outage["ts"] if outage else None
        if t_outage is not None and t_outage >= time.time():
            raise ValueError("chaos outage timestamp is in the future")
        incident = step(2, names[2], ok=True, primary=primary, target=target,
                        t_outage=t_outage, outage_evidence=(
                            "chaos/chaos-events.jsonl" if outage else None),
                        auto=auto, message="Incident opened: primary readiness outage")
        result.update(t_incident=incident["ts"], t_outage=t_outage,
                      notification_delay_s=(incident["ts"] - t_outage
                                            if t_outage is not None else None))
        if not confirm(auto, f"Approve failover from Region {primary.upper()} to {target.upper()}?"):
            result["reason"] = "operator declined cutover"
            return result

        current_step = 3
        cutover = fo.failover(target, backend, wait=60.0)
        result["failover"] = cutover
        result["cutover"] = bool(cutover.get("ok") and cutover.get("cutover"))
        step(3, names[3], ok=bool(cutover.get("ok")), result=cutover)

        current_step = 4
        replica_ok = bool(cutover.get("weights") and cutover.get("vector_count", 0) > 0)
        step(4, names[4], ok=replica_ok, weights=cutover.get("weights"),
             vector_count=cutover.get("vector_count"),
             embed_model_version=cutover.get("embed_model_version"),
             rpo_seconds=cutover.get("rpo_seconds"), docs_lost=cutover.get("docs_lost"))
        current_step = 5
        step(5, names[5], ok=result["cutover"], target=target,
             failed_step=cutover.get("failed_step"))
        if not result["cutover"] or not replica_ok:
            result["reason"] = cutover.get("reason", "cutover or replica verification failed")
            current_step = 6
            step(6, names[6], ok=False, skipped=True, reason=result["reason"])
            return result

        current_step = 6
        requests = []
        for number in range(10):
            begin = time.monotonic()
            record = {"seq": number, "ts": time.time(), "ok": False}
            try:
                response = httpx.get(f"{URL[target]}/v1/infer",
                                     params={"q": f"hoa don thang {number + 1}"}, timeout=3.0)
                body = response.json()
                if not isinstance(body, dict):
                    raise ValueError("inference response must be a JSON object")
                record.update(status=response.status_code, served_by=body.get("region"),
                              ok=response.status_code == 200 and body.get("region") == target
                              and not body.get("error"))
            except (httpx.RequestError, ValueError) as exc:
                record["reason"] = f"{type(exc).__name__}: {exc}"
            record["latency_ms"] = (time.monotonic() - begin) * 1000
            requests.append(record)
        p95 = sorted(record["latency_ms"] for record in requests)[9]
        error_rate = sum(not record["ok"] for record in requests) / 10
        result.update(ok=error_rate == 0 and p95 < P95_LIMIT_MS,
                      p95_latency_ms=p95, error_rate=error_rate)
        step(6, names[6], ok=result["ok"], p95_latency_ms=p95,
             error_rate=error_rate, p95_limit_ms=P95_LIMIT_MS, requests=requests)
        if not result["ok"]:
            result["reason"] = "golden signals failed; operator must assess rollback"
        return result
    except Exception as exc:
        result["reason"] = f"{type(exc).__name__}: {exc}"
        step(current_step, names[current_step], ok=False, reason=result["reason"])
        return result
    finally:
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        step(7, "post_incident", **result,
             measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a", choices=["a", "b"])
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    result = run(a.primary, a.target, a.backend, a.auto)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
