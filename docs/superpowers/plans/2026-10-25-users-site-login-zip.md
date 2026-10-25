# 網站登入、使用者與 ZIP 實作紀錄

依使用者補充需求直接實作；使用者確認一般帳號預設每日 5 部，管理者可在網頁調整。

- [x] `user_store.py`：SQLite 帳號、scrypt 密碼、issuer/sub 綁定、角色／停用、session 版本、原子預留與跨日完成量、個人 cookies 加密儲存。
- [x] `authentication.py`、`accounts.py`：帳密／Token／SSO 登入、管理者操作、全體預設與個別上限、密碼重設及停用撤銷 session。
- [x] `main.py`、`media.py`：個人任務與 WebSocket、單檔與重試授權、私有 cookies 自動選用、背景工作額度結算、啟動釋放孤立預留。
- [x] `site_login.py`：獨立 Chromium context、網站入口、遠端畫面、點選／鍵盤／文字／捲動、擷取 cookies、所有者驗證、逾時與資源回收、公開網路與導航檢查。
- [x] `archives.py`：目前使用者完成項目的串流 ZIP、中文及安全檔名、重名編號、Zip64、檔案占用及連線失敗釋放。
- [x] 前端：一般帳密與管理者 Token、網站登入與需要登入提示、個人網站管理、使用者／額度管理、一鍵 ZIP、個人剩餘部數、手機操作。
- [x] Docker／Compose 與 README：安裝 Chromium、保留既有服務名稱、持久儲存與備份、SSO 管理者設定、網站相容性限制。

## 驗證

- 61 項 Python 測試，涵蓋既有下載、OIDC、真實 HLS/DASH、帳號與配額、ZIP、跨使用者授權，以及實際 Chromium 登入表單、擷取 HttpOnly cookie 後下載受保護影片。
- 3 項前端回歸測試：解析中改網址、無效設定、網站要求登入時不誤登出工具。
- 瀏覽器實際操作：Token 登入、新增 Alice/Bob、全體預設 5 部、Alice 個別 2 部、一般帳密登入及管理按鈕隔離、網站拒絕存取、互動視窗手動登入、個人 cookies 自動下載、2 部達上限、第 3 部拒絕、原生 ZIP 下載且內容不重名、移除網站資料、手機畫面及可見任務卡、登出。沒有頁面執行錯誤。
- 以獨立空白 context 開啟 YouTube 入口，Google 的「登入／繼續使用 YouTube」頁正常顯示；沒有輸入真實帳號，也沒有宣稱通過 Google 的完整登入限制。
- Python 編譯、前端語法、相依套件一致性、Compose 結構檢查通過。
- 本機 FFmpeg/ffprobe 使用測試用 arm64 工具，沒有修改系統安裝。
- 未建置 Docker 映像、未部署正式 NAS，亦未使用正式 Synology／Google 憑證。

目前目錄不是 Git 儲存庫，沒有建立 worktree 或提交。
