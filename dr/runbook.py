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

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
EDGE_INFER_URL = "http://127.0.0.1:8080/v1/infer"


def step(n, name, **kw):
    """TODO: ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    line = json.dumps(record)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(f"[{n}/7] {name}: {line}")
    return record

def _read_last_chaos_ts(region: str) -> float | None:
    p = pathlib.Path("chaos/chaos-events.jsonl")
    if not p.exists():
        return None
    try:
        events = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        kills = [e for e in events if e.get("action") == "kill" and e.get("region") == region]
        return kills[-1]["ts"] if kills else None
    except Exception:
        return None


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    while True:
        reply = input(f"{msg} [y/N]: ").strip().lower()
        if reply in ("y", "yes"):
            return True
        if reply in ("n", "no", ""):
            return False
        print("  -> Vui lòng trả lời 'y' hoặc 'n'")


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook theo đúng tài liệu chuẩn."""
    t_start = time.time()
    result_summary = {
        "primary": primary,
        "target": target,
        "backend": backend,
        "status": "IN_PROGRESS",
    }

    # =========================================================================
    # Bước 1: xac_nhan_outage — Probe nhiều lần để tránh false positive / flapping
    # =========================================================================
    primary_fails = 0
    num_probes = 3

    for _ in range(num_probes):
        try:
            r = httpx.get(f"{URL[primary]}/readyz", timeout=1.5)
            if r.status_code != 200:
                primary_fails += 1
        except Exception:
            primary_fails += 1
        time.sleep(0.3)

    primary_down = primary_fails >= 2
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_fails=primary_fails,
        primary_down=primary_down,
        target=target,
    )

    # Không được phép failover chỉ vì operator chạy nhầm runbook. Nếu primary
    # không fail đủ ngưỡng, dừng trước khi mở incident hay thay đổi bất kỳ state nào.
    if not primary_down:
        result_summary["status"] = "ABORTED_PRIMARY_HEALTHY"
        result_summary["reason"] = (
            f"Region {primary} chỉ fail {primary_fails}/{num_probes} probe; "
            "không đủ bằng chứng outage"
        )
        return result_summary

    # =========================================================================
    # Bước 2: thong_bao_incident — Ghi nhận operator nhận tin và tính độ trễ
    # =========================================================================
    t_operator_known = time.time()
    t_outage = _read_last_chaos_ts(primary)
    notification_delay_s = (
        round(t_operator_known - t_outage, 3) if t_outage is not None else None
    )

    step(
        2,
        "thong_bao_incident",
        t_outage=t_outage,
        t_operator_known=t_operator_known,
        notification_delay_s=notification_delay_s,
        message=f"Alert: Region {primary} DOWN. Failover to Region {target}.",
    )

    # Human confirmation checkpoint (Bán tự động)
    if not confirm(auto, f"Trigger failover from primary '{primary}' to target '{target}'?"):
        step(2, "abort_operator_declined", reason="Operator rejected cutover")
        result_summary["status"] = "ABORTED_OPERATOR_DECLINED"
        return result_summary

    # =========================================================================
    # Bước 3: scale_gpu_pool — Gọi hàm failover.failover DUY NHẤT 1 LẦN
    # =========================================================================
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(
        3,
        "scale_gpu_pool",
        failover_result=fo_res,
        ok=fo_res.get("ok", False),
    )

    if not fo_res.get("ok"):
        step(3, "abort_failover_failed", reason="failover.failover returned ok=False")
        result_summary["status"] = "FAILED_FAILOVER"
        return result_summary

    # =========================================================================
    # Bước 4: verify_state_replica — Đọc lại state thật từ region đích
    # =========================================================================
    replica_state = {}
    try:
        state_response = httpx.get(f"{URL[target]}/v1/state", timeout=2.0)
        replica_state = state_response.json() if state_response.status_code == 200 else {
            "status": state_response.status_code,
        }
    except Exception as exc:
        replica_state = {"error": str(exc)}

    replica_ok = bool(
        replica_state.get("weights")
        and replica_state.get("count", 0) > 0
        and replica_state.get("pool_state") == "full"
    )
    step(
        4,
        "verify_state_replica",
        target=target,
        replica_ok=replica_ok,
        vector_count=replica_state.get("count"),
        weights=replica_state.get("weights"),
        pool_state=replica_state.get("pool_state"),
        rpo_seconds=fo_res.get("rpo_seconds"),
        docs_lost=fo_res.get("docs_lost"),
        embed_model_version=fo_res.get("embed_model_version"),
    )

    if not replica_ok:
        result_summary["status"] = "FAILED_REPLICA_VERIFICATION"
        result_summary["replica_state"] = replica_state
        return result_summary

    # =========================================================================
    # Bước 5: dns_cutover — Xác nhận trạng thái cutover
    # =========================================================================
    active_reg = pathlib.Path("edge/active_region").read_text(encoding="utf-8").strip()
    cutover_ok = active_reg == target

    step(
        5,
        "dns_cutover",
        active_region=active_reg,
        cutover_success=cutover_ok,
    )

    if not cutover_ok:
        result_summary["status"] = "FAILED_CUTOVER_VERIFICATION"
        return result_summary

    # =========================================================================
    # Bước 6: verify_golden_signals — Chạy 10 test requests đo p95 & error rate
    # =========================================================================
    latencies = []
    errors = 0
    total_test_requests = 10

    with httpx.Client(timeout=5.0) as client:
        for _ in range(total_test_requests):
            t0 = time.time()
            try:
                # Đi qua edge và gọi inference thật; /readyz đơn thuần không chứng
                # minh request của user đã được Region B phục vụ sau cutover.
                resp = client.get(EDGE_INFER_URL, params={"q": "dr-golden-signal"})
                lat = time.time() - t0
                body = resp.json() if resp.status_code == 200 else {}
                if resp.status_code == 200 and body.get("region") == target:
                    latencies.append(lat)
                else:
                    errors += 1
            except Exception:
                errors += 1
            time.sleep(0.1)

    latencies.sort()
    p95_idx = int(len(latencies) * 0.95)
    p95_latency = round(latencies[min(p95_idx, len(latencies) - 1)] * 1000, 2) if latencies else None
    error_rate = round(errors / total_test_requests, 2)

    step(
        6,
        "verify_golden_signals",
        requests_sent=total_test_requests,
        errors=errors,
        error_rate=error_rate,
        p95_latency_ms=p95_latency,
    )

    # =========================================================================
    # Bước 7: post_incident — Kết thúc và ghi lệnh đo RTO
    # =========================================================================
    elapsed_total = round(time.time() - t_start, 2)
    measure_cmd = "python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    final_status = "COMPLETED" if errors == 0 else "FAILED_GOLDEN_SIGNALS"

    step(
        7,
        "post_incident",
        total_elapsed_s=elapsed_total,
        measure_rto_cmd=measure_cmd,
        status=final_status,
    )

    result_summary.update({
        "status": final_status,
        "total_elapsed_s": elapsed_total,
        "error_rate": error_rate,
        "p95_latency_ms": p95_latency,
    })
    return result_summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
