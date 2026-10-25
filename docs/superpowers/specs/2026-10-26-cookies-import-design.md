# 改用 cookies.txt 匯入

使用者要求移除無法可靠登入 Google 的伺服器瀏覽器功能，保留個人 cookies.txt 匯入，並提供指定擴充套件的安裝與操作教學。本文件取代先前設計中的遠端網站登入部分；既有 SSO、下載與使用者管理維持。

## 行為

- 頁首、網址解析區及需要網站登入的失敗任務，皆提供「Cookies 設定」或教學匯入入口。
- 同一匯入視窗列出四步教學：Chrome 安裝、在影片網站登入、Netscape 格式 Export／Export As、回工具匯入後重新解析／重試。
- 安裝連結使用 https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc ，另開分頁。
- 說明 2 MB 上限、只匯出目標網站、新檔取代舊匯入檔、過期重新匯出，以及登入資料的保管。
- 移除遠端畫面、鍵盤輸入、登入開關及所有 /api/site-logins 路由。

## 實作界線

以 site_cookies.py 保留網站網域比對、登入需求判斷、個人 cookies 列表及刪除；不再啟動瀏覽器。保留已保存的加密資料與原有網站分類鍵。Docker 和正式依賴不再安裝 Playwright／Chromium；開發依賴可供介面驗證使用。

## 驗證

完整下載、SSO、額度與 ZIP 測試；cookies.txt 匯入後實際下載需 cookie 的本地影片，驗證帳號隔離與移除；確認舊遠端登入路由不可使用。桌面及手機驗證教學、安裝連結、上傳、錯誤訊息、移除及失敗任務入口。
