# Current Docstring Audit

## Snapshot

- Audit date: `2026-06-25`
- Scope: all `*.py` files under the BQuant workspace
- Remaining functions/classes without docstrings: `148`

## What was improved in this pass

Các module lõi đã được bổ sung docstring/comment theo convention mới:

- `apps/web/services/charting.py`
- `apps/web/services/operations.py`
- `apps/web/pages/dashboard.py`
- `apps/web/pages/symbol_explorer.py`
- `apps/web/pages/alerts.py`
- `apps/web/pages/operations.py`
- `data_ingestion/` entire folder
- `pipelines/live_update_worker.py`
- `pipelines/run_intraday_delta.py`
- `pipelines/run_eod_reconcile.py`
- `utils/logger.py`

Ngoài ra đã bổ sung convention tổng thể tại:

- [current_coding_conventions.md](/storage/hhbach/bquant/architectures/system/current_coding_conventions.md)

## Phần còn thiếu tập trung ở đâu

Hotspots chính hiện tại:

1. `pipelines/evaluate_alerts.py`
2. `pipelines/ingest_observability_logs.py`
3. `pipelines/live_update_runtime.py`
4. `warehouse/`
5. test doubles trong `tests/`
6. một phần helper còn lại trong `utils/`

## Ưu tiên rollout tiếp theo

### Priority 1

- `pipelines/evaluate_alerts.py`
- `pipelines/ingest_observability_logs.py`
- `pipelines/live_update_runtime.py`
- `warehouse/data_manifest.py`

### Priority 2

- `warehouse/refresh_state.py`
- `warehouse/duckdb_connection.py`
- `utils/rate_limit.py`
- `utils/logger.py` helper methods còn lại

### Priority 3

- test fakes / helper classes
- phần helper kho dữ liệu ít thay đổi hoặc ít rủi ro hơn

## Ghi chú triển khai

- Mục tiêu là docstring mô tả đúng ngữ cảnh vận hành, không thêm comment hình thức.
- Các đoạn fallback dữ liệu, checkpoint, cache invalidation, gap-fill và merge semantics nên luôn được comment ở mức block nếu logic không hiển nhiên.
