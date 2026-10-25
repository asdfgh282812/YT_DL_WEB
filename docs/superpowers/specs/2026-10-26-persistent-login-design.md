# 修正頻繁登出

## 問題與使用者目標

使用者以 Synology SSO 登入後容易看到「登入已失效」。程式存在三項已確認原因：本機 session 的期限取 SSO ID token 的 exp；session 僅放在記憶體，重啟就遺失；前端收到 WebSocket 1008 即登出，未核對 HTTP 登入狀態。截圖無法證明此次 NAS 觸發的是哪一項，修正涵蓋三種情況。

## 設計

- 每次登入建立本工具獨立的期限，預設 7 天，SESSION_MAX_AGE 可設定 1 小時到 30 天。SSO 驗證本身維持 state、nonce、簽章與有效期檢查。
- UserStore 在同一個持久 SQLite 新增 login_sessions 表；保存隨機 SID 的 SHA-256 雜湊、帳號、驗證版本、登入方式、subject 與到期時間，不存原始 SID 或 SSO token。
- 每次驗證 session 同時核對帳號停用狀態與 auth_version，載入最新角色。登出刪除當前 session，管理者撤銷會刪除該帳號所有 session。新登入會輪替當前 SID，保留其他裝置登入。
- Cookie 與資料庫沿用固定金鑰及掛載，重建後有效 session 仍可使用。舊記憶體登入不能遷移，需再登入一次。期限為登入起算的固定期限。
- WebSocket 1008 後以 /api/auth/me 確認，401 才清除畫面登入狀態；200、502 或網路失敗保留畫面並重試。確認等待期間若有新登入或登出，忽略舊回應。
- PUBLIC_BASE_URL 未填 scheme 時補 https://；來源比對處理預設 port，仍拒絕其他來源。

## 驗證

SSO 短效 token 到期後登入仍有效、模擬兩個應用實例共用資料庫完成重啟測試、主動登出防止 cookie 重播、資料庫固定到期、停用與密碼重設即時撤銷、不同裝置與 SID 輪替、來源比對，以及前端斷線各種回應與非同步競態。執行完整下載／cookies／額度／ZIP／SSO 測試與真實瀏覽器斷線、重連與重啟檢查。
