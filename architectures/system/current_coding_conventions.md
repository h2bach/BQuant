# BQuant Coding Conventions

## Mục tiêu

Tài liệu này chốt chuẩn comment, docstring và coding convention cho BQuant để việc bảo trì, debug và thay đổi pipeline/web app về sau có cùng một ngữ cảnh.

## 1. Module conventions

- Mỗi file Python cần có module docstring ngắn ở đầu file, mô tả trách nhiệm chính của module.
- Module nên giữ một trách nhiệm chính. Nếu file vừa chứa UI, vừa chứa data access, vừa chứa orchestration, cần xem đó là tín hiệu để tách nhỏ dần.
- Import nên theo nhóm:
  1. standard library
  2. third-party
  3. local packages

## 2. Function và class docstring

- Tất cả hàm public phải có docstring theo chuẩn function document, không chỉ mô tả một dòng.
- Hàm private cũng cần function document khi:
  - logic không hiển nhiên
  - hàm là helper dùng lại nhiều nơi
  - hàm thao tác với time/window/checkpoint/cache/materialization
  - hàm nhận/trả về `DataFrame`, payload JSON, DB connection, chart spec, hoặc object có contract ngầm
- Docstring phải mô tả đủ:
  - hàm làm gì trong ngữ cảnh BQuant
  - `Args`: từng input, kiểu dữ liệu kỳ vọng, ý nghĩa business, default/constraint quan trọng
  - `Returns`: kiểu dữ liệu trả về, schema/cột chính nếu là `DataFrame` hoặc cấu trúc dict/list
  - `Raises`: exception chủ động raise hoặc lỗi phổ biến mà caller cần biết
  - `Side Effects`: ghi DB/file/log/cache/network call nếu có
  - `Notes`: assumption, idempotency, timezone, source/fallback, hoặc biến trung gian quan trọng nếu không nhìn code là hiểu ngay
- Với class/dataclass, docstring cần có `Attributes` cho từng field public.
- Với CLI `main()` và `parse_args()`, docstring vẫn cần nêu rõ input từ command line và side effects của process.
- Tránh docstring dạng lặp lại tên hàm hoặc comment mọi assignment hiển nhiên.

Template khuyến nghị:

```python
def build_symbol_chart(symbol: str, mode: str = "daily", period_key: str = "1Y") -> tuple[ChartSpec, list[dict[str, str]], str]:
    """Build the chart payload used by Symbol Explorer.

    Args:
        symbol: VN30 constituent ticker in BQuant canonical uppercase form.
        mode: Dataset mode to render. Supported values are `daily` and `intraday`.
        period_key: UI range preset used to trim the loaded data before rendering.

    Returns:
        Tuple containing the chart render spec, metric cards, and a source note.

    Raises:
        ValueError: If the requested symbol/mode has no data available.

    Notes:
        Daily mode reads `daily_ohlcv_base`; intraday mode reads the merged
        base+delta intraday view so the UI can reflect live updates.
    """
```

Ví dụ tối thiểu chấp nhận được cho helper đơn giản:

```python
def load_market_overview_data(period_key: str = "1Y") -> pd.DataFrame:
    """Load market-level benchmark, breadth, and volume rows.

    Args:
        period_key: Trailing range preset such as `1Y`, `5Y`, or `ALL`.

    Returns:
        DataFrame keyed by `trading_date` with VNINDEX/VN30 OHLCV columns,
        constituent breadth columns, volume, and relative strength.
    """
```

## 3. Comment policy

- Không comment những câu hiển nhiên như “assign value”, “return dataframe”.
- Chỉ thêm inline/block comment ở các đoạn:
  - fallback dữ liệu
  - coalesce nhiều nguồn
  - gap-fill / watermark / checkpoint
  - rate-limit / retry / idempotency
  - cache invalidation / refresh version
  - logic business theo phiên giao dịch
- Comment nên giải thích “vì sao”, không chỉ “điều gì”.
- Không cần comment mọi biến cục bộ. Chỉ comment biến trung gian khi biến đó thể hiện contract quan trọng, ví dụ `chart_frame`, `price_lines`, `payload`, `watermark`, `delete_range`, `checkpoint`, hoặc schema tạm thời dùng qua nhiều bước.

## 4. Naming

- Biến thời gian phải rõ ràng:
  - `trading_date`: ngày giao dịch daily
  - `bar_time`: timestamp intraday
  - `session_date`: ngày phiên cho intraday
  - `*_ts`: timestamp
- Các hàm load/prepare/build nên giữ nghĩa ổn định:
  - `load_*`: lấy dữ liệu từ DB/file/source
  - `prepare_*`: transform dữ liệu cho use case cụ thể
  - `build_*`: dựng figure/UI payload/output object
  - `run_*`: orchestration job/pipeline
- Private helper dùng prefix `_`.

## 5. DataFrame conventions

- Trước khi tính indicator hoặc compare giá trị, luôn ép numeric bằng `pd.to_numeric(..., errors="coerce")`.
- Khi dùng price series để autoscale hoặc indicator, loại các giá trị `<= 0` nếu đó là series giá.
- Các cột OHLCV dùng tên chuẩn:
  - `open`, `high`, `low`, `close`, `volume`
- Nếu một series là normalized/rebased, tên cột phải thể hiện rõ:
  - `normalized_close`
  - `vnindex_plot_close`
  - `local_basket_close`

## 6. Logging conventions

- Không dùng `print(...)` trong pipeline/web/runtime.
- Dùng `BQuantLogger` cho toàn bộ event có ý nghĩa vận hành.
- Mỗi log structured nên có đủ bối cảnh khi phù hợp:
  - `run_id`
  - `trigger_type`
  - `dataset_name`
  - `symbol`
  - `event_type`
  - `status`
- Với fallback/gap-fill/coalesce, luôn log lý do và số lượng affected rows/days nếu tính được.

## 7. Web/UI conventions

- Page module chỉ nên lo UI composition và user interaction.
- Query/trigger logic dùng service layer nếu có thể.
- Với plot:
  - indicator mặc định tắt nếu dễ gây rối chart
  - autoscale phải dựa trên vùng đang nhìn thấy
  - chart title/note phải nói rõ source đang được dùng và fallback nào đang diễn ra

## 8. Pipeline conventions

- Mọi job chạy độc lập phải:
  - có `parse_args()`
  - có `main()`
  - log structured khi start/end/fail
  - idempotent hoặc nêu rõ phần không idempotent
- Các hàm ghi DB/file cần thể hiện rõ semantics:
  - append
  - replace
  - upsert
  - trim
  - merge

## 9. Testing conventions

- Test name mô tả hành vi, không chỉ mô tả function name.
- Với logic live update/market time, ưu tiên test deterministic bằng fixed time inputs.
- Với chart/data transforms, test ít nhất:
  - schema/traces chính
  - no-crash path
  - fallback path quan trọng

## 10. Ưu tiên rollout

Do codebase hiện còn thiếu docstring ở nhiều module, nên rollout nên theo thứ tự:

1. `apps/web/services/`
2. `apps/web/pages/`
3. `utils/logger.py`
4. `pipelines/live_update_*`
5. `data_ingestion/`
6. `warehouse/`

Mục tiêu không phải “comment cho đủ”, mà là tạo ra một codebase có thể đọc, debug và thay đổi mà không phải đoán ngữ cảnh.
