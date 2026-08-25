# Runbook 1 trang — Region chính down

Runbook phục vụ xử lý sự cố lúc 3h sáng bởi kỹ sư trực on-call. Mỗi bước có câu lệnh copy-paste được, tín hiệu nhận biết hoàn thành, và vai trò thực hiện.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage, chống false positive | `1..3 \| ForEach-Object { python chaos/kill_region.py status; Start-Sleep -Seconds 5 }` | Cả 3 kết quả liên tiếp đều cho `a.ready=false`; Region B vẫn `alive=true` | SRE On-call |
| 2 | Mở incident, bấm giờ và xác nhận cutover | `python dr/runbook.py --primary a --target b --backend fs` | Có dòng `thong_bao_incident`, sau đó chương trình hỏi `Trigger failover ... [y/N]`; Incident Commander nhập `y` | Incident Commander |
| 3 | Restore snapshot (do lệnh bước 2 thực hiện đúng một lần) | `Get-Content reports/failover-events.jsonl -Tail 5` | Có `1_verify_target` rồi `2_restore_snapshot` với `rpo_seconds`, `docs_lost`, `embed_model_version` | SRE Automation |
| 4 | Scale pool và verify replica | `curl.exe http://127.0.0.1:8002/v1/state` | `pool_state="full"`, `weights=true`, `count>0`; tiếp theo `4_wait_ready` có `ok=true` | SRE Automation |
| 5 | Xác nhận DNS/LB cutover | `curl.exe http://127.0.0.1:8080/edge/state` | `active_region="b"`; log có `5_dns_cutover` sau `4_wait_ready` | SRE On-call |
| 6 | Verify golden signals qua inference thật | `Get-Content reports/runbook-run.jsonl -Tail 2` | Dòng `verify_golden_signals` ghi `requests_sent=10`, `errors=0`, `error_rate=0.0`, có `p95_latency_ms`; request được gửi qua Edge và phục vụ bởi B | SRE On-call |
| 7 | Đo RTO và mở postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid=true`, `warnings=[]`, `recovered_by_region="b"`, `rto_verdict="PASS"`, RTO ≤ 300s | Incident Commander |

> Chỉ bước 2 làm thay đổi hệ thống và nó gọi chuỗi failover đúng **một lần**. Các lệnh bước 3–6 chỉ xác minh kết quả; không restore, scale hay cutover lần thứ hai. Không dùng `--auto` trong vận hành thật — cờ đó chỉ dành cho drill/CI.

**Rollback (failover ngược về Region A):**
- **Điều kiện Rollback:** Chỉ thực hiện trả traffic về Region A khi:
  1. Region A đã được sửa lỗi triệt để, hoạt động ổn định và endpoint `/readyz` trả về HTTP 200 liên tục trong ít nhất 15 phút.
  2. Toàn bộ dữ liệu mới ghi nhận tại Region B trong thời gian sự cố đã được đồng bộ ngược (reverse-replication) đầy đủ về Region A để tránh mất mát dữ liệu (RPO=0).
  3. Có sự phê duyệt trực tiếp của **Incident Commander / Lead SRE** để tránh hiện tượng flapping (chuyển vùng qua lại liên tục gây gián đoạn dịch vụ).

- **Lệnh failback sau khi đủ cả ba điều kiện:**
  1. Chụp state mới nhất từ B: `python state/snapshot.py put --region b --backend fs`
  2. Restore sang A: `python state/snapshot.py get --region a --backend fs`
  3. Scale A: `python -c "import pathlib; pathlib.Path('state/region-a/pool_state').write_text('full', encoding='utf-8')"`
  4. Chỉ khi `curl.exe http://127.0.0.1:8001/readyz` trả HTTP 200 liên tục 15 phút, Incident Commander mới cutover: `python -c "import pathlib; pathlib.Path('edge/active_region').write_text('a', encoding='utf-8')"`
  5. Xác nhận `curl.exe http://127.0.0.1:8080/edge/state` trả `active_region="a"`; nếu inference lỗi hoặc state A/B lệch, lập tức ghi lại `b` vào `edge/active_region`.
