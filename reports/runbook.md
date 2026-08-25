# Runbook 1 trang — Region chính (a) down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` — kiểm tra `a.alive`/`a.ready`; hoặc chờ `dr/health_checker.py` (đang chạy nền) tự confirm | `reports/health-events.jsonl` có dòng `event:state_change, region:a, to:UNHEALTHY` với `consecutive_fails >= threshold` — 1 lần fail KHÔNG tính | On-call engineer |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` (không `--auto`, sẽ hỏi xác nhận) | ts của dòng `step:2, name:thong_bao_incident` ghi vào `reports/runbook-run.jsonl` — đây là mốc 0 cho đồng hồ postmortem | On-call engineer |
| 3 | Restore state ở region phụ | Tự động — bước 2 gọi `failover.failover()`, bên trong nó chạy `python3 state/snapshot.py get --region b --backend fs` | `reports/failover-events.jsonl` có dòng `step:2_restore_snapshot` kèm `rpo_seconds`, `docs_lost`, `embed_model_version` khác null | Tự động (bởi `dr/failover.py`) |
| 4 | Scale pool warm→full | Tự động — `failover.failover()` bước `3_scale_pool` ghi `full` vào `state/region-b/pool_state` | `curl localhost:8002/readyz` trả `200` và `"ready": true` | Tự động |
| 5 | DNS/LB cutover | Tự động — CHỈ chạy sau khi bước 4 xác nhận `/readyz` 200 (nếu timeout thì `dr/failover.py` tự abort, không cutover) | `curl localhost:8080/edge/state` cho `active_region:"b"`, và `reports/failover-events.jsonl` có dòng `step:5_dns_cutover` | Tự động |
| 6 | Verify golden signals | Tự động — `dr/runbook.py` gửi 10 request thật vào `/v1/infer` của region b | `reports/runbook-run.jsonl` dòng `step:6` cho `error_rate: 0.0` và `p95_latency_ms` ở mức mili-giây (không phải timeout) | On-call xem lại log sau khi bước tự động chạy xong |
| 7 | Đo RTO + viết postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"rto_verdict": "PASS"` và `"valid": true`, `"warnings": []` | On-call, sau đó bàn giao cho người viết postmortem |

**Rollback (failover ngược về region A):** Chỉ trả traffic về A sau khi (1) `a.readyz` trả
`200` liên tục ≥ 3 lần poll (cùng ngưỡng chống-flap như bước 1), VÀ (2) đã chạy lại
`state/replicate.py`/`snapshot.py put --region b` để đồng bộ dữ liệu B → A trước khi cutover
ngược — nếu không sẽ mất chính dữ liệu vừa ghi vào B trong lúc A down. **Ai quyết định:**
Incident Commander (không phải on-call một mình) — theo §4 Anti-Patterns, full-auto rollback
không có circuit breaker sẽ khiến 2 region flap qua lại nếu A chỉ "gần" ổn định.
