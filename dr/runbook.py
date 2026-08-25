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
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def _health_checker_detected(region: str):
    """Dòng state_change UNHEALTHY gần nhất của `region` trong log health_checker,
    nếu có. Đây là nguồn dò outage CHÍNH THỨC (đã chống flap qua threshold)."""
    if not HEALTH_LOG.exists():
        return None
    hits = []
    for line in HEALTH_LOG.read_text().splitlines():
        if not line.strip():
            continue
        e = json.loads(line)
        if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                and e.get("region") == region):
            hits.append(e)
    return hits[-1] if hits else None


def _confirm_outage(primary: str, wait_for_health_s: float = 90.0, poll: float = 1.0,
                     fallback_attempts: int = 3, fallback_interval: float = 5.0):
    """Ưu tiên CHỜ health_checker (đã chống flap, là nguồn dò chính thức của hệ thống)
    xác nhận UNHEALTHY — cutover chỉ nên xảy ra sau khi automation thật sự phát hiện,
    không phải sau khi operator tự tay probe nhanh hơn health_checker. Chỉ probe độc
    lập (fallback) khi health-events.jsonl không có gì, vd chạy runbook một mình mà
    không có health_checker song song.
    """
    deadline = time.time() + wait_for_health_s
    while time.time() < deadline:
        hit = _health_checker_detected(primary)
        if hit:
            return True, {"source": "health_checker", "event": hit}
        time.sleep(poll)
    fails = 0
    for i in range(fallback_attempts):
        ready, reason = hc.probe(primary, timeout=2.0)
        if not ready:
            fails += 1
        if i < fallback_attempts - 1:
            time.sleep(fallback_interval)
    return fails == fallback_attempts, {"source": "fallback_probe",
                                        "attempts": fallback_attempts, "fails": fails}


def step(step_no, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": step_no, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans == "y"


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước của runbook §4 "Region Chính Down"."""
    result = {"primary": primary, "target": target}

    # 1) xac_nhan_outage — chờ health_checker (chống flap) xác nhận UNHEALTHY; không
    #    tin 1 lần fail, và không tự probe nhanh hơn automation chính thức.
    outage_confirmed, detect_info = _confirm_outage(primary)
    step(1, "xac_nhan_outage", region=primary, confirmed=outage_confirmed, **detect_info)
    result["outage_confirmed"] = outage_confirmed

    # 2) thong_bao_incident — mốc "operator biết tin", LUÔN sau t_outage thật
    t_incident = time.time()
    step(2, "thong_bao_incident", region=primary, t_incident=t_incident,
         note="operator xac nhan incident, bat dau clock")
    result["t_incident"] = t_incident

    if not confirm(auto, f"Region {primary} co ve DOWN (nguon: {detect_info.get('source')}). "
                          f"Bat dau failover sang {target}?"):
        step(2, "huy_boi_operator", region=primary)
        result["ok"] = False
        result["reason"] = "operator_declined"
        return result

    # 3) scale_gpu_pool — gọi failover.failover(...) ĐÚNG MỘT LẦN, hàm đó tự làm
    #    5 bước con và tự ghi log riêng vào reports/failover-events.jsonl.
    fo_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, backend=backend,
         failover_ok=fo_result.get("ok"))
    result["failover"] = fo_result

    # 4) verify_state_replica — CHỈ đọc lại kết quả bước 3, không gọi lại failover
    step(4, "verify_state_replica", target=target,
         rpo_seconds=fo_result.get("rpo_seconds"), docs_lost=fo_result.get("docs_lost"),
         restore_meta=fo_result.get("restore_meta"))

    # 5) dns_cutover — cũng chỉ đọc lại: cutover có ok hay không
    step(5, "dns_cutover", target=target, cutover=fo_result.get("cutover", False))

    if not fo_result.get("ok"):
        step(6, "verify_golden_signals", skipped=True, reason="failover khong thanh cong")
        step(7, "post_incident", elapsed_s=round(time.time() - t_incident, 2), ok=False)
        result["ok"] = False
        result["reason"] = fo_result.get("reason", "failover_failed")
        return result

    # 6) verify_golden_signals — 10 request thật vào region phụ
    latencies, errors = [], 0
    for i in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0,
                          params={"q": f"hoa don thang {i % 12 + 1}"})
            ok = r.status_code == 200 and r.json().get("region") == target
        except Exception:
            ok = False
        if not ok:
            errors += 1
        latencies.append((time.time() - t0) * 1000)
    latencies.sort()
    p95 = latencies[int(0.95 * (len(latencies) - 1))] if latencies else None
    error_rate = errors / len(latencies) if latencies else None
    step(6, "verify_golden_signals", target=target, n=len(latencies),
         p95_latency_ms=round(p95, 1) if p95 is not None else None, error_rate=error_rate)
    result["golden_signals"] = {"p95_latency_ms": p95, "error_rate": error_rate}

    # 7) post_incident — tổng kết + lệnh đo RTO
    elapsed = round(time.time() - t_incident, 2)
    step(7, "post_incident", elapsed_s=elapsed, target=target,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300")
    result["ok"] = True
    result["elapsed_s"] = elapsed
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
