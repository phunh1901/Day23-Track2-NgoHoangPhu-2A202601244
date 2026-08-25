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
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """TODO: append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **kw,
    }
    line = json.dumps(record)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(line)
    return record


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước failover đúng thứ tự."""
    target_url = URL[target]
    primary = "a" if target == "b" else "b"

    # 1: verify_target — /v1/state của region phụ
    state_data = {}
    try:
        r = httpx.get(f"{target_url}/v1/state", timeout=2.0)
        state_data = r.json() if r.status_code == 200 else {"status": r.status_code}
    except Exception as e:
        state_data = {"error": str(e)}

    emit(step="1_verify_target", target=target, state=state_data)

    # 2: restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
    snap_meta = snapshot.get(target, backend)
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    target_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    rpo_info = snapshot.rpo(primary_db, target_db)

    rpo_seconds = rpo_info.get("rpo_seconds")
    docs_lost = rpo_info.get("docs_lost")
    embed_model_version = snap_meta.get("embed_model_version", "unknown")

    emit(
        step="2_restore_snapshot",
        target=target,
        backend=backend,
        rpo_seconds=rpo_seconds,
        docs_lost=docs_lost,
        embed_model_version=embed_model_version,
    )

    # 3: scale_pool — ghi "full" vào state/region-<t>/pool_state
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full", encoding="utf-8")

    emit(step="3_scale_pool", target=target, pool_state="full")

    # 4: wait_ready — POLL /readyz tới khi 200
    start_poll = time.time()
    ready = False

    while time.time() - start_poll < wait:
        try:
            r = httpx.get(f"{target_url}/readyz", timeout=1.5)
            if r.status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)

    waited = round(time.time() - start_poll, 2)
    if not ready:
        emit(step="4_wait_ready", target=target, ok=False, waited_s=waited, error="timeout")
        return {"ok": False, "target": target, "reason": f"target not ready after {wait}s"}

    emit(step="4_wait_ready", target=target, ok=True, waited_s=waited)

    # 5: dns_cutover — ghi region đích vào edge/active_region
    active_region_file = pathlib.Path("edge/active_region")
    active_region_file.parent.mkdir(parents=True, exist_ok=True)
    active_region_file.write_text(target, encoding="utf-8")

    emit(step="5_dns_cutover", target=target, active_region=target)

    return {
        "ok": True,
        "target": target,
        "backend": backend,
        "rpo_seconds": rpo_seconds,
        "docs_lost": docs_lost,
        "embed_model_version": embed_model_version,
        "warmup_waited_s": waited,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
