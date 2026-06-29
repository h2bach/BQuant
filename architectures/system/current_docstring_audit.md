# Current Docstring Audit

## Snapshot

- Audit date: `2026-06-26`
- Scope: all `*.py` files under the BQuant workspace
- Total functions/classes scanned: `419`
- Remaining functions/classes without docstrings: `148`
- Remaining one-line function docstrings needing function-document expansion: `142`

## What was improved in this pass

Các module lõi đã được bổ sung hoặc nâng cấp docstring/comment theo convention mới:

- `apps/web/services/charting.py`
- `apps/web/pages/dashboard.py`
- `apps/web/pages/symbol_explorer.py`
- `data_ingestion/vnstock_adapter.py`
- `data_ingestion/fetch_daily_10y_base.py`
- `data_ingestion/fetch_market_index_daily_10y.py`
- `data_ingestion/fetch_intraday_15m.py`
- `pipelines/reset_market_data.py`

Các module đã có docstring/comment từ pass trước và vẫn cần kiểm tra sâu dần:

- `apps/web/services/operations.py`
- `apps/web/pages/alerts.py`
- `apps/web/pages/operations.py`
- `pipelines/live_update_worker.py`
- `pipelines/run_intraday_delta.py`
- `pipelines/run_eod_reconcile.py`
- `utils/logger.py`

Ngoài ra đã bổ sung convention tổng thể tại:

- [current_coding_conventions.md](/storage/hhbach/bquant/architectures/system/current_coding_conventions.md)

## Phần còn thiếu tập trung ở đâu

Hotspots chính hiện tại:

1. `pipelines/`: `55` missing docstrings, `18` one-line docstrings
2. `warehouse/`: `48` missing docstrings
3. `tests/`: `30` missing docstrings
4. `utils/`: `15` missing docstrings, `16` one-line docstrings
5. `data_ingestion/`: `66` one-line docstrings còn lại ở các module legacy/validation
6. `apps/`: `33` one-line docstrings ở các page/service chưa qua pass mới

## Ưu tiên rollout tiếp theo

### Priority 1

- `pipelines/evaluate_alerts.py`
- `pipelines/ingest_observability_logs.py`
- `pipelines/live_update_runtime.py`
- `warehouse/data_manifest.py`
- `pipelines/run_intraday_delta.py`
- `pipelines/run_eod_reconcile.py`

### Priority 2

- `warehouse/refresh_state.py`
- `warehouse/duckdb_connection.py`
- `utils/rate_limit.py`
- `utils/logger.py` helper methods còn lại
- `data_ingestion/validate_ohlcv.py`
- các module ingestion legacy không còn nằm trên path `vnstock` `VCI-data-source`

### Priority 3

- test fakes / helper classes
- phần helper kho dữ liệu ít thay đổi hoặc ít rủi ro hơn

## Ghi chú triển khai

- Mục tiêu là docstring mô tả đúng ngữ cảnh vận hành, không thêm comment hình thức.
- Các đoạn fallback dữ liệu, checkpoint, cache invalidation, gap-fill và merge semantics nên luôn được comment ở mức block nếu logic không hiển nhiên.
- Chuẩn mới là function-document style: `Args`, `Returns`, `Raises`, `Side Effects`, `Notes` khi phù hợp. Không chấp nhận thêm mới docstring một dòng cho hàm public.
- Pass ngày `2026-06-26` cũng mở rộng chart indicator suite trong `apps/web/services/charting.py`; các docstring mới phải mô tả rõ DataFrame schema và payload contract để tránh lỗi scale/chart về sau.
