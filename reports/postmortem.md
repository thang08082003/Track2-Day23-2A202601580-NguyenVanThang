# Postmortem — DR Drill Lab 23 (Drill 2, region a → region b)

Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | +s từ t_outage | Sự kiện | Evidence |
|---|---|---|---|
| 2026-08-25T11:35:59 | 0 | outage bắt đầu (`chaos/kill_region.py --region a --mode netblock --mock`) | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T11:35:59 | +0.1s | user đầu tiên bị ảnh hưởng (`ok:false`, `ReadTimeout` 2006.4ms) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T11:36:18 | +19.1s | health check alert (`to:UNHEALTHY, region:a, consecutive_fails:3`) | `reports/health-events.jsonl:2` |
| 2026-08-25T11:36:18 | +19.1s | operator/automation nhận alert, gọi runbook, bấm giờ incident | `reports/runbook-run.jsonl` step `2 thong_bao_incident` |
| 2026-08-25T11:36:24 | +25.4s | cutover ghi `edge/active_region=b` | `reports/failover-events.jsonl:5` |
| 2026-08-25T11:36:27 | +28.3s | resolved — request đầu tiên OK, `served_by:"b"` | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.3s` · gap: **âm 271.7s** (vượt mục tiêu rất xa — không phải vấn đề)
- RPO mục tiêu: không có target cứng trong bài (báo cáo bắt buộc, không phải pass/fail) · đo được: `8.02s` (`4` doc bị mất, `reports/failover-events.jsonl:2`)
- **Bước tốn nhiều giây nhất:** health-check detection (19.1s / 28.3s tổng, ~68%). Kế đến là GPU pool warm-up (6.2s, ~22%). Snapshot restore và DNS cutover gần như tức thời (backend `fs`, TTL edge chỉ cộng thêm ~2.9s ở cuối) — vì sao detection chiếm nhiều nhất: `interval_s(5) × threshold(3) = 15.0s` là sàn lý thuyết bắt buộc để chống flapping; con số thực tế 19.1s còn cộng thêm độ lệch pha giữa lúc kill và lần poll kế tiếp của health checker.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. Tại sao user thấy lỗi? → Vì `edge/proxy.py` vẫn route traffic vào region a dù nó đã treo (SIGSTOP).
2. Tại sao edge không tự tránh region a? → Vì `edge/proxy.py` chỉ đọc `edge/active_region`, không tự health-check — nó tin tưởng vào cơ chế bên ngoài (`dr/health_checker.py` + `dr/failover.py`) để cập nhật file đó.
3. Tại sao mất tới 19.1s để phát hiện? → Vì ngưỡng chống-flap `interval=5s, threshold=3` yêu cầu 3 lần fail liên tiếp mới được coi là outage thật — đây là đánh đổi có chủ đích, không phải lỗi.
4. Tại sao mất thêm 6.2s sau khi restore xong mới ready? → Vì `serving/app.py` mô phỏng GPU pool warm-up (`WARMUP_SECONDS=6`) — pool chuyển `warm→full` không có nghĩa là sẵn sàng phục vụ ngay.
5. Tại sao mất `docs_lost=4` document? → Vì `state/replicate.py` chạy mỗi 30s (`--every 30`), còn `state/ingest.py` ghi liên tục — khoảng cách giữa lần snapshot cuối và lúc outage luôn để lại một cửa sổ dữ liệu chưa kịp replicate. **Đây chính là định nghĩa của RPO**, không phải bug.

**Kết luận:** không có bước nào trong runbook "thất bại" ở lần chạy này — toàn bộ 28.3s là chi phí có chủ đích của kiến trúc (chống flap + warm-up + replication lag), không phải lỗi vận hành.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giảm `replicate.py --every` từ 30s xuống 10s để giảm cửa sổ mất dữ liệu | Data platform | Sprint tới | Giảm RPO trung bình từ ~15s xuống ~5s (không ảnh hưởng RTO) |
| 2 | Đánh giá lại `WARMUP_SECONDS` với model thật (hiện đang mock = 6s cố định) trước khi áp dụng số này cho production | Infra/MLOps | Trước go-live | Không giảm ngay, nhưng tránh báo cáo RTO sai lệch so với thực tế |
| 3 | Test kịch bản `--mode stop` (SIGKILL, fail nhanh) thay vì chỉ `netblock`, để so sánh RTO khi lỗi là "chết hẳn" vs "treo" | On-call/SRE | 2 tuần | Không giảm RTO của kịch bản này, nhưng phủ thêm loại lỗi chưa đo |

## 5. Ba câu hỏi bắt buộc trả lời

1. `interval × threshold` của tôi là `5.0s × 3 = 15.0s` (sàn lý thuyết). Nó chiếm khoảng
   **53%** của mục tiêu RTO 300s tính theo sàn lý thuyết, nhưng so với RTO **thực đo** 28.3s
   thì detection thực tế (19.1s) chiếm **~68%** — đây là thành phần lớn nhất trong toàn bộ RTO.
2. Nếu hạ `interval` xuống 1s (giữ `threshold=3`), sàn lý thuyết còn 3s, có thể kéo RTO xuống
   quanh 12-15s. Cái giá phải trả: health checker poll `/readyz` của cả 2 region 5 lần nhiều
   hơn mỗi giây — tăng tải lên chính endpoint mà bạn đang cố cứu, và nhạy hơn với các lỗi
   mạng thoáng qua (packet loss ngắn, GC pause) khiến hệ thống dễ flap qua lại giữa 2 region
   dù không có outage thật (§4 Anti-Patterns).
3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` không còn là
   "4 tài liệu trễ vài giây" nữa — nó là **4 giao dịch/ticket khách hàng biến mất vĩnh viễn**,
   không thể phục hồi từ bất kỳ đâu vì đó là bản ghi mới nhất và region a (nguồn) đã chết.
   Với tần suất `replicate.py --every 30s`, 6 giờ outage với ingest liên tục 0.5 doc/s có thể
   làm `docs_lost` lên tới hàng chục nghìn bản ghi nếu operator không kịp chuyển sang chế độ
   ingest kép (ghi đồng thời cả 2 region) trước khi hết cửa sổ dữ liệu chưa replicate.
