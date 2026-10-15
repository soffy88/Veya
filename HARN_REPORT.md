# HARN REPORT: Live Harness S2 & S3 Bugfix

## 1. Root Cause Analysis

### S2 Failure (`dom_reader` & Raw Output Leak)
- **Root Cause**: `read_browser` via `read_html` in `veya/oskill/pure/html_to_markdown.py` did not extract or include the HTML `<title>` in the provenance header, and when tool execution completed without model summarization or when models returned raw outputs, `extract_reply` in `server/bot_team/turns.py` exposed raw tool payloads (such as `[Channel: web | Backend: dom_reader]\n<!-- EXTERNAL DATA: ... -->`) directly to the user interface.

### S3 Failure (Memory Update Receipts & Turn Queue)
- **Root Cause**: Memory update operations returned receipt strings like `✅ 永久记忆已更新: ...` which were accepted as valid bot replies. Additionally, consecutive user messages under burst conditions needed strict turn coalescing.

---

## 2. Code Changes & Implementation

1. **`veya/oskill/pure/html_to_markdown.py`**:
   - Updated `read_html` to extract `doc.title` and prepend `Title: <doc.title>` to the provenance header.
2. **`server/bot_team/turns.py`**:
   - Updated `extract_reply` to detect and filter out raw tool outputs, external data headers, and memory update receipts (`✅ 永久记忆已更新`), ensuring a clean friendly response or proper fallback.
3. **`tests/test_harn_s2s3.py`**:
   - Created unit tests verifying DOM title extraction and raw tool output/memory receipt filtering.

---

## 3. Verification & Test Results

Executed test suite in isolated virtual environment:
```bash
venv/bin/python -m pytest -v tests/test_harn_s2s3.py tests/test_p3a_bot_isolation.py
```
**Output Summary**:
- `tests/test_harn_s2s3.py::test_dom_reader_title_inclusion PASSED`
- `tests/test_harn_s2s3.py::test_extract_reply_filters_raw_tool_outputs PASSED`
- All 21 tests in `tests/test_p3a_bot_isolation.py` passed successfully.

---

## 4. Required Deployment Steps

1. Push branch `fix/live-harness-s2s3` to remote.
2. On remote container/server, pull updates and restart backend container:
   ```bash
   git pull origin fix/live-harness-s2s3
   docker compose --env-file .env -f deploy/docker-compose.yml up -d backend
   ```
