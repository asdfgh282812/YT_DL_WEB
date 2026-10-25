# 通用媒體下載 Implementation Plan

**Goal:** 修復 HLS 影片辨識、真正同時下載，加入格式與 cookies 來源選擇及 Synology OIDC。

**Architecture:** 保留 FastAPI/Vue，將格式/cookies 與登入拆成獨立模組，主程式集中任務與 API。

**Tech Stack:** Python、FastAPI、yt-dlp、FFmpeg、Authlib、Vue 3。

依使用者授權在本次對話直接執行；目前資料夾沒有 Git 儲存庫，不建立 worktree 或提交。

- [x] `media.py`：有效 HTTP URL、Netscape 驗證、cookies 快照、無高度影片、格式選擇、完成檔案辨識。
- [x] `main.py`：背景解析、動態併發排程、設定範圍驗證、Referer/UA、重試、活動任務清理保護。
- [x] `authentication.py`：OIDC discovery/code/query、可選 PKCE、subject allowlist、signed HttpOnly session、同源檢查、API/WS/檔案驗證。
- [x] `frontend/app.js` 與 `frontend/index.html`：登入、格式、串流選項、來源狀態/測試/刪除、重試與實際活動數。
- [x] `tests/`：以 threading.Event 阻擋模擬下載，斷言最大活動值與變更；本地 FFmpeg 產生 HLS/TS 與 MPD fixture，下載後 ffprobe 檢查影片和音軌；模擬 OIDC discovery/JWKS/token 完成登入。
- [x] `README.md`、`.env.example`、`Dockerfile`、`compose.yaml`：啟動方式與獨立 OIDC client 註冊說明，不重用參考專案的 secret。
- [x] 執行 `python -m pytest tests -q`、`node --check frontend/app.js`，啟動隔離測試實例，透過瀏覽器驗證登入、設定、格式和重試。

## 實際驗證紀錄

- 35 項 Python 測試通過（含 9 項 OIDC 情境、本地 HLS/TS、MPD、HTTP 直連與實際音軌/影像驗證）。
- 2 項前端回歸測試通過：解析中的來源變更、設定驗證失敗仍保留視窗。
- Chromium 實際操作：錯誤/正確 Token、HttpOnly session、WebSocket、設定範圍、cookies 來源切換、HLS 解析與 MP4 下載、來源改變後停用按鈕、失敗重試、390px 版面、設定與登出。沒有頁面執行錯誤。
- JavaScript 語法、Python 編譯及 Python 相依套件檢查通過。
- 本機原有 FFmpeg 為 x86_64，測試機為 arm64；驗證使用 /tmp 下獨立的 arm64 FFmpeg/ffprobe，沒有替換系統程式。
- Docker daemon 在目前環境未啟動，因此沒有建置映像或更新 NAS 的正式服務。SSO 用模擬 IdP 驗證，尚未以正式 Synology client 登入。
