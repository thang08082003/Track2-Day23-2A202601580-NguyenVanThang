# RTO/RPO Evidence — Lab 23

Mỗi con số ở đây trỏ về một dòng log thật (`đường/dẫn.jsonl:số_dòng`), lấy từ đúng
lần chạy `make drill-baseline` / `make drill-dr` mới nhất (sau `make clean`).

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T11:33:24` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage (timeout ReadTimeout 2005.7ms) | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage trong toàn bộ cửa sổ drill | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl` | `reports/measure-drill-1.json` |

16/32 request thất bại (`requests_failed: 16` trong `reports/measure-drill-1.json`), khớp
`pytest tests/test_rto_evidence.py::test_drill1_ton_tai_va_khong_phuc_hoi`.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.1s | dòng `ok:false` đầu (ReadTimeout 2006.4ms) | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 19.1s | `to:UNHEALTHY, region:a, consecutive_fails:3` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 19.2s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 25.4s | `step:4_wait_ready, ready:true` | `reports/failover-events.jsonl:4` |
| DNS cutover | 25.4s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 28.3s | dòng `ok:true` đầu (served_by:"b") sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.3s` | 300s (5 phút) | **PASS** |
| RPO — Vector DB | `8.02s` / `4` doc | — (báo cáo, không có mục tiêu cứng) | đo được, không ước lượng |

`tools/measure_rto.py` xác nhận `"valid": true`, `"warnings": []`, `"rto_verdict": "PASS"`,
`"recovered_by_region": "b"` (khác `"killed_region": "a"`) — xem `reports/measure-drill-2.json`.

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 19.1s | Sàn lý thuyết là `interval_s(5.0) × threshold(3) = 15.0s` (`reports/health-events.jsonl:2`); thực tế 19.1s vì lần poll đầu tiên không trùng đúng lúc kill — lệch pha polling cộng thêm ~4s | Giảm `interval` hoặc `threshold` — nhưng threshold thấp (vd 1) làm health checker flap theo mọi lỗi mạng thoáng qua, không phải outage thật |
| Snapshot restore | 0.1s | `2_restore_snapshot` gần như tức thời vì dùng backend `fs` (copy file cục bộ) — `reports/failover-events.jsonl:2`, từ +19.1s xuống +19.2s | Đã tối thiểu với backend `fs`; backend `minio` qua mạng thật sẽ chậm hơn, đổi lại benchmark thực tế hơn |
| GPU pool warm-up | 6.2s | Từ lúc `pool_state` chuyển `warm→full` (+19.2s) tới lúc `/readyz` trả 200 (+25.4s) — đúng bằng `WARMUP_SECONDS=6` mặc định của `serving/app.py` | Giảm `WARMUP_SECONDS`, nhưng con số này mô phỏng thời gian GPU thật nạp model — giảm giả tạo là gian lận số liệu |
| DNS/LB TTL cache | 2.9s | Từ lúc `5_dns_cutover` ghi `edge/active_region=b` (+25.4s) tới request đầu tiên của loadgen đi qua đúng region mới (+28.3s) — chờ `edge/proxy.py` hết cache `EDGE_TTL_SECONDS=5` | Giảm `EDGE_TTL_SECONDS`, đánh đổi bằng việc edge phải đọc file/DNS thường xuyên hơn |

Tổng: `19.1 + 0.1 + 6.2 + 2.9 = 28.3s` — khớp `rto_measured_s` trong `reports/measure-drill-2.json`.
