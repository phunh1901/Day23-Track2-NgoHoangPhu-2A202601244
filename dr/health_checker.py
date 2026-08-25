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
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """TODO: trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    url = f"{URL[region]}/readyz"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                return True, "OK (200)"
            return False, f"HTTP {resp.status_code}: {resp.text[:100].strip()}"
    except httpx.ConnectTimeout:
        return False, "Connection Timeout"
    except httpx.ReadTimeout:
        return False, "Read Timeout"
    except httpx.ConnectError as e:
        return False, f"Connection Refused/Failed ({type(e).__name__})"
    except Exception as e:
        return False, f"Error: {str(e)}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out.parent.mkdir(parents=True, exist_ok=True)

    state = {r: "HEALTHY" for r in URL}
    consecutive_fails = {r: 0 for r in URL}
    consecutive_passes = {r: 0 for r in URL}

    start_time = time.time()

    with open(out, "a", encoding="utf-8") as f:
        while time.time() - start_time < duration:
            loop_start = time.time()

            for region in URL:
                is_ready, reason = probe(region, timeout)

                if is_ready:
                    consecutive_passes[region] += 1
                    consecutive_fails[region] = 0

                    if state[region] == "UNHEALTHY":
                        state[region] = "HEALTHY"
                        event = {
                            "ts": time.time(),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                            "event": "state_change",
                            "region": region,
                            "from": "UNHEALTHY",
                            "to": "HEALTHY",
                            "reason": reason,
                            "interval_s": interval,
                            "threshold": threshold,
                            "consecutive_passes": consecutive_passes[region],
                        }
                        f.write(json.dumps(event) + "\n")
                        f.flush()
                else:
                    consecutive_fails[region] += 1
                    consecutive_passes[region] = 0

                    if state[region] == "HEALTHY" and consecutive_fails[region] >= threshold:
                        state[region] = "UNHEALTHY"
                        event = {
                            "ts": time.time(),
                            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                            "event": "state_change",
                            "region": region,
                            "from": "HEALTHY",
                            "to": "UNHEALTHY",
                            "reason": reason,
                            "interval_s": interval,
                            "threshold": threshold,
                            "consecutive_fails": consecutive_fails[region],
                        }
                        f.write(json.dumps(event) + "\n")
                        f.flush()

            elapsed = time.time() - loop_start
            sleep_time = max(0.0, interval - elapsed)
            time.sleep(sleep_time)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
