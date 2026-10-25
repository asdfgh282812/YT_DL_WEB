# 修正頻繁登出執行計畫

目標：維持登入狀態並避免把即時連線失敗誤判為登出。

架構：authentication.py 管理本機獨立期限與 signed cookie，user_store.py 持久保存可撤銷的雜湊 session；frontend/app.js 在 WebSocket 1008 後核對 HTTP 狀態。

- [x] user_store.py、authentication.py：加入持久 session、固定登入期限及即時帳號核對，處理輪替與撤銷，保留 SSO 驗證。
- [x] frontend/app.js：HTTP 確認真正失效；連線錯誤重連，舊確認回應不影響新登入。
- [x] .env.example、README.md：說明 SESSION_MAX_AGE、7 天固定期限、掛載、舊登入遷移與 IdP 登出界線。
- [x] tests/test_sessions.py、tests/test_oidc.py、tests/test_frontend.cjs：加入重啟、期限、撤銷、短效 ID token、來源與前端競態測試；相關 43 項 Python 與 9 項前端測試通過。
- [x] 完整回歸及真實瀏覽器驗證，檢查測試服務已關閉。

目前資料夾不是 Git repository，不建立提交，不同步其他較舊副本。NAS 套用更新需重新建置容器，升級後舊登入需重新登入一次。

驗證結果：77 項 Python 測試、9 項前端測試通過。真實瀏覽器驗證 WebSocket 拒絕後保留登入並重連、HTTP 502 不登出、實際後端程序重啟及重新整理保留登入、撤銷後才返回登入頁；無頁面錯誤。測試服務程序已由 finally 終止並等待結束。程式語法與 Compose 設定檢查通過。SSO-only 模式升級後仍拒絕過去的帳密登入 session。
