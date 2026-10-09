# RTO/RPO Evidence — Lab 23

Số đo lấy từ log thật qua `tools/measure_rto.py`; timestamp epoch được đối chiếu
trong cửa sổ loadgen của từng drill. Không dùng elapsed_s của runbook làm user RTO.

## 1. Drill 1 — baseline không có DR

| Chỉ số | Giá trị | Evidence |
|---|---|---|
| Outage UTC | 2026-10-09T02:59:56 | `chaos/chaos-events.jsonl:1` |
| User thấy lỗi đầu tiên | +0.1s | `reports/drill-1-nodr.jsonl:17` |
| Request fail | 14 | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY trong cửa sổ đo; không có request phục hồi sau lỗi | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

Outage UTC: 2026-10-09T03:27:10; mode netblock; region A bị ảnh hưởng, region B phục hồi.

| Mốc | Giây từ outage | Evidence |
|---|---|---|
| Outage | +0.0s | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +2.0s | `reports/drill-2-withdr.jsonl:26` |
| Health check phát hiện | +14.2s | `reports/health-events.jsonl:2` |
| Verify target | +17.9s | `reports/failover-events.jsonl:3` |
| Snapshot restore xong | +18.1s | `reports/failover-events.jsonl:4` |
| Region phụ ready | +27.6s | `reports/failover-events.jsonl:6` |
| DNS cutover | +27.6s | `reports/failover-events.jsonl:7` |
| Request phục hồi từ B / RTO | +29.6s | `reports/drill-2-withdr.jsonl:38` |

| Chỉ số | Đo được | Mục tiêu | Verdict / Evidence |
|---|---|---|---|
| RTO inference | 29.6s | 300s | PASS; `reports/measure-drill-2.json` |
| RPO vector DB | 6.01s; 3 docs mất | 300s | PASS về thời gian; `reports/failover-events.jsonl:4` |
| Request fail | 12 | Giảm số request bị ảnh hưởng | `reports/measure-drill-2.json` |

RTO còn dư 270.4s; RPO còn dư 293.99s so với mục tiêu.
Embedding model đã restore: `embed-model=vi-e5-base@v3`.
RPO so sánh dữ liệu primary và bản restore tại thời điểm restore, không phải tuổi snapshot.

## 3. Thành phần thời gian

| Thành phần | Đo được | Evidence / Diễn giải | Cách giảm |
|---|---|---|---|
| Ngân sách detect theo lab | 5s × 3 = 15s; thực đo 14.2s | `reports/health-events.jsonl:2` | Giảm interval, cân nhắc tải probe và flapping |
| Detect → verify target | 3.69s | `reports/health-events.jsonl:2` → `reports/failover-events.jsonl:3` | Tránh lặp xác nhận không cần thiết; giữ quyền duyệt của operator |
| Verify → restore xong | 0.22s | `reports/failover-events.jsonl:3` → `reports/failover-events.jsonl:4` | Giữ snapshot sẵn, tối ưu I/O |
| Restore → scale | 0.008s | `reports/failover-events.jsonl:4` → `reports/failover-events.jsonl:5` | Giảm thao tác điều phối |
| Scale → ready xác nhận | 9.50s | `reports/failover-events.jsonl:5` → `reports/failover-events.jsonl:6` | Warm pool; bao gồm warmup, poll và request xác minh |
| Ready → cutover | 0.019s | `reports/failover-events.jsonl:6` → `reports/failover-events.jsonl:7` | Ghi pointer nguyên tử |
| Cutover → user thành công | 1.97s | `reports/failover-events.jsonl:7` → `reports/drill-2-withdr.jsonl:38` | Bao gồm cache proxy, nhịp loadgen và request; không tách được DNS TTL thật |

15s là công thức ngân sách của lab, không phải cận dưới tuyệt đối: ba lần fail
cách nhau 5s trải qua 10s từ lần fail đầu, cộng độ lệch pha outage và độ trễ request.
Vì vậy thời gian detect thực đo 14.2s không mâu thuẫn với polling; không sửa timestamp.
Baseline thiếu health/cutover là dự kiến vì không có DR; drill 2 valid=true, warnings rỗng.
