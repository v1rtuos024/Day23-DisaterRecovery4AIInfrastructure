# Postmortem — DR Drill Lab 23

Ngày diễn tập: 09/10/2026. Phạm vi: hai region giả lập local, bare mode,
`netblock --mock`, snapshot backend `fs`. Phân tích blameless tập trung vào
hệ thống và quy trình, không quy trách nhiệm cá nhân.

Region A ngừng phản hồi; dịch vụ phục hồi qua B sau 29.6s, đạt RTO 300s.
Có 12 request thất bại. Bản restore thiếu 3 tài liệu so với primary tại thời
điểm restore, tương ứng RPO 6.01s. Drill có `valid=true`, `warnings=[]` và
`rto_verdict=PASS` (`reports/measure-drill-2.json:1`).

## 1. Timeline

Thời gian ISO dưới đây là UTC (giờ Việt Nam cộng 7 giờ), làm tròn đến millisecond
từ timestamp log. Mốc outage là điểm bắt đầu tính RTO.

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T03:27:10.529Z | Outage bắt đầu: A bị SIGSTOP; process B vẫn sống | `chaos/chaos-events.jsonl:3` |
| 2026-10-09T03:27:12.523Z | Request lỗi đầu tiên sau outage: HTTP 503 / ReadTimeout, latency 2051.9ms; +2.0s | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T03:27:24.724Z | Health checker báo A UNHEALTHY sau 3 lỗi liên tiếp; +14.2s | `reports/health-events.jsonl:2` |
| 2026-10-09T03:27:28.311Z | Runbook hoàn tất 3 probe xác nhận outage; B còn warm, thiếu weights và vector | `reports/runbook-run.jsonl:1` |
| 2026-10-09T03:27:28.320Z | Mở incident, tiếp tục theo --auto; không có xác nhận thủ công của operator; trễ thông báo 17.79s | `reports/runbook-run.jsonl:2` |
| 2026-10-09T03:27:28.411Z | Verify target B trước restore | `reports/failover-events.jsonl:3` |
| 2026-10-09T03:27:28.634Z | Restore xong: RPO 6.01s, thiếu 3 docs, embedding model vi-e5-base@v3 | `reports/failover-events.jsonl:4` |
| 2026-10-09T03:27:28.642Z | Chuyển pool B sang full | `reports/failover-events.jsonl:5` |
| 2026-10-09T03:27:38.146Z | B ready, có weights và 216 vector documents | `reports/failover-events.jsonl:6` |
| 2026-10-09T03:27:38.166Z | Cutover edge sang B; +27.6s | `reports/failover-events.jsonl:7` |
| 2026-10-09T03:27:39.184Z | 10 request trực tiếp B thành công, error rate 0%, p95 143.95ms | `reports/runbook-run.jsonl:6` |
| 2026-10-09T03:27:40.136Z | Request phục hồi đầu tiên qua edge, served_by=b; RTO +29.6s | `reports/drill-2-withdr.jsonl:38` |

Kiểm tra trực tiếp B thành công trước khi loadgen qua edge phục hồi. Thời điểm
kết thúc runbook hoặc cutover không thay thế được RTO của người dùng.

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

| Chỉ số | Mục tiêu | Đo được | Gap (mục tiêu trừ đo được) | Evidence |
|---|---|---|---|---|
| RTO inference | 300s | 29.6s, PASS | Còn dư 270.4s | `reports/drill-2-withdr.jsonl:38` |
| RPO vector DB tại restore | 300s | 6.01s; 3 docs thiếu | Còn dư 293.99s về thời gian | `reports/failover-events.jsonl:4` |

Baseline không có DR có 14 request lỗi và NO_RECOVERY trong cửa sổ đo
(`reports/measure-drill-1.json:1`). Không suy ra downtime ngoài cửa sổ đó hoặc
so sánh trực tiếp số request lỗi của hai cửa sổ khác nhau.

| Thành phần RTO | Khoảng thực đo | Evidence |
|---|---:|---|
| Outage → health checker phát hiện | 14.195s | `chaos/chaos-events.jsonl:3` → `reports/health-events.jsonl:2` |
| Phát hiện → verify target (xác nhận/điều phối) | 3.687s | `reports/health-events.jsonl:2` → `reports/failover-events.jsonl:3` |
| Verify → restore snapshot xong | 0.222s | `reports/failover-events.jsonl:3` → `reports/failover-events.jsonl:4` |
| Restore → scale pool | 0.008s | `reports/failover-events.jsonl:4` → `reports/failover-events.jsonl:5` |
| Scale → ready (warm-up, polling, xác minh) | 9.505s | `reports/failover-events.jsonl:5` → `reports/failover-events.jsonl:6` |
| Ready → cutover | 0.019s | `reports/failover-events.jsonl:6` → `reports/failover-events.jsonl:7` |
| Cutover → request thành công qua edge | 1.970s | `reports/failover-events.jsonl:7` → `reports/drill-2-withdr.jsonl:38` |

Tổng khoảng đã làm tròn là 29.606s, phù hợp RTO 29.6s. Bước tốn nhiều giây nhất
là phát hiện outage (14.195s, khoảng 48% RTO): phải chờ chu kỳ probe và timeout
để đủ 3 lỗi liên tiếp. Khoảng cuối gồm cache DNS/LB giả lập, nhịp loadgen và xử
lý request; log không tách riêng được TTL.

RPO so sánh latest_doc_ts primary 1791516448.2594647 với bản restore
1791516442.2541008, chênh 6.01s. Ingest và replication local vẫn chạy khi serving
A bị SIGSTOP; snapshot dùng để restore được tạo sau outage
(`reports/replication.jsonl:2`). Kết quả này chưa chứng minh RPO khi mất toàn bộ
region và nguồn dữ liệu không còn truy cập được.

## 3. Root cause (5 whys)

1. **Vì sao người dùng gặp lỗi?** Edge gửi traffic đến A đang ngừng phản hồi,
   dẫn đến ReadTimeout (`reports/drill-2-withdr.jsonl:26`).
2. **Vì sao không chuyển sang B ngay?** Cần xác nhận nhiều lỗi liên tiếp và
   chuẩn bị target trước cutover; B thiếu weights, vector và pool full
   (`reports/runbook-run.jsonl:1`). Standby này có thời gian phục hồi.
3. **Vì sao process B sống nhưng chưa phục vụ được?** Readiness inference phụ
   thuộc compute, model và dữ liệu. Process sống hoặc /healthz không bảo đảm
   những điều kiện này (`reports/failover-events.jsonl:3`).
4. **Vì sao phải restore và warm-up trong incident?** Dữ liệu được sao lưu định
   kỳ, pool dự phòng ở trạng thái warm, chưa sẵn sàng trước sự cố
   (`reports/replication.jsonl:1`, `reports/failover-events.jsonl:5`).
5. **Vì sao còn khoảng trống đảm bảo DR?** Drill chỉ dừng serving, vẫn đọc được
   primary và tạo snapshot sau outage. Khi mất region thật, restore có thể thiếu
   artifact và phép đo RPO đọc DB primary sẽ thất bại. Cần kiểm thử phục hồi
   độc lập với primary và kiểm tra artifact trước incident.

Một sự kiện trước drill ghi lỗi thiếu `state/region-b/weights/VERSION` tại
restore (`reports/failover-events.jsonl:2`). Log chưa đủ để kết luận nguyên
nhân; cần preflight artifact/version. Sự kiện này không thuộc RTO của drill
thành công.

## 4. Action items (đề xuất, chưa triển khai)

Owner là vai trò đề xuất; deadline tính từ ngày diễn tập. Mức giảm là giả
thuyết/ngân sách cần đo lại, không phải kết quả đã đạt.

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây / tiêu chí xác minh |
|---|---|---|---|---|
| 1 | Thử interval 1s, threshold 3; đo cả timeout, lỗi thoáng qua và flapping | SRE | 2026-10-12 | Ngân sách detect giảm 15s → 3s (12s); đo RTO thực và số chuyển vùng sai |
| 2 | Preflight snapshot bằng restore thử: kiểm tra weights, VERSION, vector count, manifest | ML Platform | 2026-10-13 | Chưa định lượng giây; phát hiện artifact thiếu/không tương thích trước outage |
| 3 | Thử replication mỗi 5s thay vì 30s, đo chi phí I/O | Data Platform | 2026-10-14 | Chu kỳ danh nghĩa giảm 25s; cần đo lại RPO và docs_lost, chưa hứa mức giảm từ 6.01s |
| 4 | Đánh giá giữ B full với dữ liệu sẵn, so sánh chi phí compute | ML Platform + SRE | 2026-10-16 | Có thể giảm một phần scale→ready 9.505s; đo lại, không cộng cơ học các mức tiết kiệm |
| 5 | Diễn tập mất serving và quyền truy cập state A; dùng replica và nguồn đối soát ghi bền vững ngoài A | Data Platform + SRE | 2026-10-16 | Chưa định lượng; restore và đo RPO được khi A không truy cập được |
| 6 | Kiểm tra qua edge sau cutover; rà soát quyền rollback và đồng bộ trước failback | Incident Commander + SRE | 2026-10-13 | Chưa định lượng; xác nhận end-to-end, chỉ failback khi A ready và dữ liệu đã đối soát |

## 5. Ba câu hỏi bắt buộc trả lời

1. **interval × threshold là bao nhiêu giây? Chiếm bao nhiêu % RTO?**

   Với interval=5s, threshold=3, ngân sách theo công thức lab là 15s: khoảng
   50.7% RTO đo được 29.6s, hoặc 5% mục tiêu 300s. Phát hiện thực tế 14.195s
   chiếm khoảng 48% RTO. Ba probe cách nhau 5s trải qua 10s từ probe đầu đến
   probe thứ ba, cộng độ lệch pha outage và thời gian request/timeout; 15s
   không phải cận dưới tuyệt đối. Nếu các bước còn lại cần B giây, ngân sách
   đơn giản yêu cầu interval <= (300-B)/3, kèm dự phòng timeout/điều phối.
   Health checker phải độc lập với serving để vẫn cảnh báo khi serving dừng.

2. **Hạ interval xuống 1s giảm RTO mấy giây và trả giá gì?**

   Ngân sách theo công thức lab giảm 15s → 3s, tiết kiệm danh nghĩa 12s.
   Chưa chạy cấu hình này nên không khẳng định RTO giảm đúng 12s. Timeout
   probe mặc định 2s dài hơn interval 1s; checker có thể bỏ lỡ slot polling.
   Tần suất danh nghĩa tăng 5 lần, tăng tải probe; 3 lỗi trong cửa sổ ngắn
   hơn dễ phản ứng với sự cố thoáng qua. Cần thử timeout/flapping và giữ
   quyền duyệt cutover trong vận hành thực tế.

3. **Outage 6 giờ và A mất dữ liệu vĩnh viễn: docs_lost nghĩa gì với khách hàng?**

   Ba docs trong drill là chênh lệch primary và bản restore tại thời điểm
   restore, chưa chứng minh mất vĩnh viễn vì DB primary vẫn tồn tại. Nếu A
   mất hoàn toàn và không có nguồn nhập lại, tài liệu chưa nằm trong replica
   không còn truy xuất được, làm câu trả lời AI thiếu hoặc cũ. Sáu giờ outage
   không tự động là mất sáu giờ dữ liệu: mức mất phụ thuộc replica cuối còn
   dùng được và nơi nhận ghi sau failover. Cần đối soát tài liệu đã xác nhận
   ghi, phục hồi từ nguồn bền vững, chuyển ingest sang vùng còn hoạt động và
   đồng bộ trước failback. Không dùng 3 docs của drill này để cam kết mức mất
   dữ liệu khi toàn region bị phá hủy.
