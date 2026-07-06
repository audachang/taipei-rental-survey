# 台北萬華區租屋比較 (Streamlit)

貼上 591 租屋連結即可即時抓取、解析、加入比較；可移除任一物件；用下載／上傳 `listings.json` 保存資料。

## 檔案
- `app.py` — Streamlit 主程式（591 解析器 + 卡片 + 比較表 + 新增/移除 + 下載/上傳）
- `listings.json` — 種子資料（目前 5 案）
- `requirements.txt` — 相依套件

## 本機執行
```bash
pip install -r requirements.txt
streamlit run app.py
```

## 部署到 Streamlit Community Cloud（免費、可公開分享）
1. 把本資料夾（`app.py`、`listings.json`、`requirements.txt`）推到一個 GitHub repo。
2. 到 https://share.streamlit.io → **New app** → 選該 repo、branch，主檔案填 `app.py`。
3. 部署後會得到 `https://<app-name>.streamlit.app` 公開網址。

## 資料持久化（方案 A）
Streamlit Community Cloud 的檔案系統為暫時性，新增/移除只存在當次連線。
要永久生效：在側欄按 **下載目前 listings.json**，再把它 commit 覆蓋 repo 中的 `listings.json`，重新部署後即成為新的預設資料。

## 新增物件解析欄位
標題、地址、租金、格局、可使用坪數、樓層、電梯、裝潢、陽台、押金、服務費、寵物政策、租金補貼、代表照片。
（解析依 591 目前頁面結構；若日後 591 改版導致某欄抓不到，會顯示空白或 —，可再調整 `parse_listing()`。）
