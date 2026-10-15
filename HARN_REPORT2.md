# HARN Report: Live Harness S2/S3 Fixes (Round 1 & Round 2)

## Overview
This report documents the root-cause analysis, code modifications, test results, and deployment instructions for repairing live harness S2 and S3 failures in the `veya-harn` worktree (`fix/live-harness-s2s3`).

---

## 1. Round 1: S2 / S3 Root Causes & Fixes
- **S2 Root Cause:** When the `dom_reader` tool fetched an external webpage (e.g. `https://example.com`), the HTML converter omitted the `<title>` tag from the rendered markdown output, and when the model failed or returned raw tool outputs directly, `extract_reply` fell back to raw tool output strings (`[Channel: web | Backend: dom_reader]...`).
- **S3 Root Cause:** When recalling or updating memory (e.g. storing "Always call the user 小林"), the raw internal receipt (`✅ 永久记忆已更新: ...`) was occasionally returned as the final bot reply. Additionally, burst messages needed proper coalescing via `VEYA_TURN_QUEUE=1`.
- **Fixes Applied:**
  1. Updated HTML-to-markdown conversion in `veya/oskill/pure/html_to_markdown.py` to extract and include `Title: <title>` at the top of rendered doc markdown alongside `Source URL:`.
  2. Enhanced `extract_reply` in `server/bot_team/turns.py` to detect and filter out raw tool output strings, external data payloads, and memory update receipts (`✅ 永久记忆已更新`), replacing them with clean fallback messages.
  3. Added comprehensive unit tests in `tests/test_harn_s2s3.py`.

---

## 2. Round 2: S3 Language Consistency Fix
- **S3 Issue (Round 2):** Even after successful memory recall and reply generation, the model occasionally answered in English (e.g., `"Your name is Xiao Lin and you live in Hangzhou."`) when the user prompt or memory content was in Chinese, whereas harness assertions required Chinese characters (`"小林"`, `"杭州"`).
- **Fix Applied:**
  1. Updated `build_objective` in `server/bot_team/turns.py` to append a clear system/objective directive instructing the model: `"请使用中文（与用户消息或回忆的语言保持一致）简洁、准确地回答用户。"`.
  2. Added unit test `tests/test_harn_round2.py` verifying that objectives include the language constraint.

---

## 3. Test Results
All related test suites passed successfully:
- `.venv/bin/python -m pytest -q tests/test_harn_round2.py tests/test_harn_s2s3.py tests/test_w11_s2_s3_landing.py` -> **5 passed**
- `.venv/bin/python -m pytest -q tests/test_p3a_bot_isolation.py` -> **19 passed**

---

## 4. Required Deployment Steps
Since `server/` is mounted read-only into the backend container, apply changes via:
```bash
docker compose --env-file .env -f deploy/docker-compose.yml up -d backend
```
