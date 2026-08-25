# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Nguyên tắc Blameless: tập trung phân tích hệ thống, quy trình và độ trễ kiến trúc, không đổ lỗi cá nhân.

## 1. Timeline (mọi dòng trỏ về evidence path:line thật)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T10:19:46 | Outage bắt đầu (Region A bị netblock) | `chaos/chaos-events.jsonl:8` |
| 2026-08-25T10:19:46 | User đầu tiên bị ảnh hưởng (ReadTimeout 503) | `reports/drill-2-withdr.jsonl:24` |
| 2026-08-25T10:20:01 | Health Check alert (chuyển sang UNHEALTHY sau 3 lần fail) | `reports/health-events.jsonl:1` |
| 2026-08-25T10:20:07 | Operator nhận tin và kích hoạt Runbook failover | `reports/runbook-run.jsonl:2` |
| 2026-08-25T10:20:08 | DNS Cutover hoàn tất chuyển sang Region B | `reports/failover-events.jsonl:5` |
| 2026-08-25T10:20:11 | Resolved (request đầu tiên thành công từ Region B) | `reports/drill-2-withdr.jsonl:34` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300.0s · đo được: `24.9s` · gap: **275.1s** (vượt chỉ tiêu an toàn).
- RPO mục tiêu: 300.0s · đo được: `8.01s` (`4` doc bị mất) · gap: **291.99s**.
- **Bước tốn nhiều giây nhất:** Health Check Detection Floor `15.0s`, chiếm `60.24%` RTO. Sau khi health alert ở +15.2s, operator kích hoạt runbook ở +21.6s, tạo thêm khoảng `6.3s` phản ứng/xác nhận (`reports/health-events.jsonl:1`, `reports/runbook-run.jsonl:2`).
- Breakdown khép kín: detect floor `15.0s` + operator/orchestration/restore `6.8s` + ready/warm-up `0.2s` + DNS cache `2.9s` = **24.9s** (`reports/rto-evidence.md`).

## 3. Root cause (5 whys)

1. *Tại sao user gặp lỗi 503?* Vì Region A bị netblock nên Edge vẫn trỏ tới A nhưng upstream không trả lời (`chaos/chaos-events.jsonl:8`, `reports/drill-2-withdr.jsonl:24`).
2. *Tại sao Edge chưa chuyển ngay sang Region B?* Vì failover cần đủ ba health-check fail liên tiếp và xác nhận bán tự động để tránh flapping; `edge/active_region` chỉ được đổi sau khi target ready.
3. *Tại sao phát hiện mất 15.2s?* Cấu hình là `interval=5s`, `threshold=3`, tương ứng detect floor 15.0s (`reports/health-events.jsonl:1`).
4. *Tại sao tổng thời gian còn tăng thêm gần 9.7s sau detect floor?* Operator/orchestration/restore chiếm 6.8s, target ready thêm 0.2s và Edge TTL thêm 2.9s. Log verify ban đầu cho thấy B đã có `pool_state=full`, `weights=true`, `count=285`; vì vậy không được mô tả drill này như một lần cold-start GPU (`reports/failover-events.jsonl:1`). Restore vẫn cần chạy để nạp snapshot nhất quán mới nhất.
5. *Tại sao mất 4 documents?* Chu kỳ snapshot 30s khiến bốn document được ingest trong 8.01s sau snapshot cuối chưa có trong bản restore (`reports/failover-events.jsonl:2`). Trong sự cố thật, snapshot store phải được sao chép liên vùng độc lập với Region A; nếu cùng failure domain, bước restore sẽ thất bại.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Tự động page on-call và chuẩn bị sẵn lệnh runbook ngay khi health checker đủ threshold; vẫn giữ nút xác nhận cutover | SRE Team | 2026-09-05 | Giảm khoảng phản ứng operator từ 6.3s xuống < 2s, tiết kiệm >4s RTO mà không biến thành full-auto |
| 2 | Triển khai CDC cho Vector DB thay cho snapshot theo chu kỳ 30s | Data Team | 2026-09-15 | Giảm RPO từ 8.01s xuống <1s và docs lost từ 4 xuống gần 0 |
| 3 | Giảm Edge TTL từ 5s xuống 2s sau khi đánh giá tải control plane | Platform Team | 2026-09-20 | Với drill này, giảm phần DNS cache từ 2.9s xuống tối đa khoảng 2s |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - `interval × threshold = 5.0s × 3 = 15.0s`.
   - Nó chiếm $15.0 / 24.9 \approx 60.24\%$ tổng thời gian RTO đo được.
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - Nếu hạ interval xuống 1s (threshold 3), thời gian phát hiện giảm từ 15s xuống 3s, giúp RTO giảm được **12 giây**.
   - Cái giá phải trả: Rất dễ bị **flapping** (chuyển đổi vùng liên tục khi mạng chỉ bị nghẽn vài gói tin trong 2-3 giây), gây ra tải đột biến và phân mảnh dữ liệu giữa 2 region.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - `docs_lost = 4` có nghĩa là 4 tài liệu/giao dịch mới nhất được người dùng gửi trong 8.01 giây trước sự cố sẽ bị mất hoàn toàn và không thể truy hồi. Khách hàng sẽ phải nhập lại các thông tin/ticket này.
