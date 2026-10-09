"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import math
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Probe readiness with a bounded HTTP timeout."""
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except httpx.RequestError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if response.status_code == 200:
        return True, "readyz HTTP 200"
    return False, f"readyz HTTP {response.status_code}: {response.text[:500]}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll both regions; append and flush only state transitions."""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("interval must be finite and positive")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if not isinstance(threshold, int) or threshold < 1:
        raise ValueError("threshold must be a positive integer")
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration must be finite and nonnegative")

    states = {region: "HEALTHY" for region in URL}
    failures = {region: 0 for region in URL}
    out.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + duration
    next_poll = started
    with out.open("a", encoding="utf-8") as log, ThreadPoolExecutor(
        max_workers=len(URL)
    ) as executor:
        while time.monotonic() < deadline:
            delay = min(next_poll, deadline) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if time.monotonic() >= deadline:
                break
            # Submit both requests before waiting so one timeout cannot block
            # the other region's probe.
            pending = {r: executor.submit(probe, r, timeout) for r in URL}
            for region, future in pending.items():
                ready, reason = future.result()
                failures[region] = 0 if ready else failures[region] + 1
                target = "HEALTHY" if ready else "UNHEALTHY"
                if target == states[region]:
                    continue
                if not ready and failures[region] < threshold:
                    continue
                previous = states[region]
                states[region] = target
                log.write(json.dumps({
                    "ts": time.time(), "event": "state_change",
                    "region": region, "from": previous, "to": target,
                    "reason": reason, "interval_s": interval,
                    "threshold": threshold,
                    "consecutive_fails": failures[region],
                }, ensure_ascii=False) + "\n")
                log.flush()
            next_poll += interval
            # Skip missed slots instead of issuing a burst after slow probes.
            now = time.monotonic()
            if next_poll < now:
                next_poll += (math.floor((now - next_poll) / interval) + 1) * interval


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
