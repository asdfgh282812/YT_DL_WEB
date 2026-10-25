# cookies.txt 教學與移除遠端登入執行計畫

目標：按使用者要求改用個人 cookies.txt 匯入，移除伺服器瀏覽器，提供指定套件連結與使用教學。

架構：site_cookies.py 保留既有網站判斷與個人資料管理；main.py 維持原有匯入、下載和加密儲存。前端統一以 Cookies 設定視窗提供教學與匯入，Docker 移除瀏覽器依賴。

- [x] frontend/app.js、frontend/index.html：移除登入控制、畫面與輪詢，調整解析／任務入口，加入教學與上傳。
- [x] main.py、authentication.py、accounts.py、site_cookies.py：拆除瀏覽器生命週期和路由，保留 cookies 分域使用、帳號隔離與刪除。
- [x] Dockerfile、requirements.txt、compose.yaml、docker-compose.yml、.env.example、README.md：移除正式瀏覽器依賴與設定，更新安裝與部署說明。
- [x] tests/test_site_cookies.py、tests/test_frontend.cjs：驗證受保護影片、舊資料、隔離、移除和教學入口，執行完整測試與桌面／手機介面檢查。

不建立 Git 提交：目前工作資料夾沒有 Git repository；不修改另一份較舊的 /Users/andy/YT_DL_WEB。

驗證結果：70 項 Python 測試及 4 項前端測試通過；桌面 1280px 與手機 390px 完成教學、連結、錯誤／成功匯入、移除、任務與解析提示檢查，無瀏覽器頁面錯誤。Compose 設定與程式語法檢查通過。尚未在 NAS 重建容器；需執行 docker compose -f compose.yaml up -d --build。
