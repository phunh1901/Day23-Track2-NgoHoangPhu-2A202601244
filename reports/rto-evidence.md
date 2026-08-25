# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:40:34` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+2.4s` | dòng `ok:false` đầu tiên có timestamp sau t_outage | `reports/drill-1-nodr.jsonl:34`, `reports/measure-drill-1.json:13` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json:14` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json:22` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0.0s | `action:kill` | `chaos/chaos-events.jsonl:8` |
| User thấy lỗi đầu tiên | 0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:24` |
| Health check phát hiện | 15.2s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:1` |
| Snapshot restore xong | 21.8s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 22.0s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 22.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 24.9s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:34` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `24.9s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `8.01s` / `4` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` tại `reports/health-events.jsonl:1` = 5.0s × 3 | Giảm interval hoặc threshold (trả giá bằng nguy cơ flapping) |
| Snapshot restore + thời gian operator/orchestration | 6.8s | restore hoàn tất ở +21.8s (`reports/failover-events.jsonl:2`) trừ detect floor 15.0s (`reports/health-events.jsonl:1`); phần này gồm thời gian operator xác nhận rồi mới restore | Page operator ngay khi health alert đủ ngưỡng; tự động chuẩn bị snapshot nhưng vẫn giữ xác nhận cutover bán tự động |
| GPU pool warm-up | 0.2s | `waited_s:0.21` tại `reports/failover-events.jsonl:4`, làm tròn theo bảng 0.1s | Duy trì hot standby hoặc preload model/pool |
| DNS/LB TTL cache | 2.9s | request B đầu tiên +24.9s (`reports/drill-2-withdr.jsonl:34`) trừ cutover +22.0s (`reports/failover-events.jsonl:5`) | Hạ TTL hoặc dùng Global Anycast Load Balancer |

Kiểm tra tổng: **15.0 + 6.8 + 0.2 + 2.9 = 24.9s**, đúng bằng RTO đo từ trải nghiệm người dùng. Trong 6.8s của bước restore/orchestration, operator nhận tin ở +21.6s (`reports/runbook-run.jsonl:2`), cho thấy phần lớn khoảng này là độ trễ phản ứng chứ không phải thời gian copy snapshot thuần túy.
