"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import math
import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append a timestamped event and show the same event to the operator."""
    ts = time.time()
    line = json.dumps({**kw, "ts": ts,
                       "iso": datetime.fromtimestamp(ts, timezone.utc).isoformat()},
                      ensure_ascii=False)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as log:
        log.write(line + "\n")
        log.flush()
    print(line, flush=True)


def state_of(region: str) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=2.0)
    response.raise_for_status()
    state = response.json()
    if state.get("region") != region:
        raise ValueError("target returned a different region")
    return state


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore and warm the standby before atomically switching traffic."""
    if target not in URL or backend not in {"fs", "minio"}:
        raise ValueError("invalid target or backend")
    if not math.isfinite(wait) or wait <= 0:
        raise ValueError("wait must be finite and positive")
    started = time.monotonic()
    active = pathlib.Path("edge/active_region")
    primary = "b" if target == "a" else "a"
    directory = pathlib.Path(f"state/region-{target}")
    result = {"ok": False, "target": target, "primary": primary}
    step = "1_verify_target"
    try:
        if active.exists() and active.read_text().strip() == target:
            raise ValueError("target already serves traffic; refusing to overwrite its state")
        before = state_of(target)
        if before.get("pool_state") not in {"cold", "warm", "full"}:
            raise ValueError("invalid target pool_state")
        # Missing weights or an empty index are repairable by the next step.
        emit(step=step, target=target, ok=True, state=before)

        step = "2_restore_snapshot"
        manifest = snapshot.get(target, backend)
        version = manifest.get("embed_model_version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("snapshot has no embedding model version")
        if manifest.get("source_region", primary) != primary:
            raise ValueError("snapshot does not belong to the primary region")
        if (directory / "weights" / "VERSION").read_text().strip() != version:
            raise ValueError("restored embedding model version differs from manifest")
        rpo = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                           directory / "vectors.sqlite")
        result.update(rpo, embed_model_version=version)
        emit(step=step, target=target, ok=True, embed_model_version=version, **rpo)

        step = "3_scale_pool"
        (directory / "pool_state").write_text("full\n")
        emit(step=step, target=target, ok=True, pool_state="full")

        step = "4_wait_ready"
        deadline = time.monotonic() + wait
        reason = "readiness deadline expired"
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                response = httpx.get(f"{URL[target]}/readyz",
                                     timeout=min(2.0, remaining))
                if response.status_code == 200 and time.monotonic() <= deadline:
                    break
                reason = f"readyz HTTP {response.status_code}: {response.text[:500]}"
            except httpx.RequestError as exc:
                reason = f"{type(exc).__name__}: {exc}"
            time.sleep(max(0.0, min(1.0, deadline - time.monotonic())))
        else:
            raise TimeoutError(f"target not ready after {wait}s: {reason}")
        restored = state_of(target)
        if not restored.get("weights") or restored.get("count", 0) < 1:
            raise ValueError("restored target has no weights or vectors")
        result.update(state=restored, weights=restored["weights"],
                      vector_count=restored["count"])
        emit(step=step, target=target, ok=True, state=restored)

        step = "5_dns_cutover"
        active.parent.mkdir(parents=True, exist_ok=True)
        # Replace the pointer so the proxy never observes a partial write.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=active.parent,
                                             delete=False) as pointer:
                temporary = pathlib.Path(pointer.name)
                pointer.write(target + "\n")
                pointer.flush()
                os.fsync(pointer.fileno())
            temporary.replace(active)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        result.update(ok=True, cutover=True)
        emit(step=step, target=target, ok=True)
    except (Exception, SystemExit) as exc:
        result.update(failed_step=step, reason=str(exc))
        emit(step=step, target=target, ok=False, reason=str(exc))
    result["elapsed_s"] = round(time.monotonic() - started, 3)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    result = failover(a.target, a.backend, a.wait)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
