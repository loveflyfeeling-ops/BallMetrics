# BallMetrics AI － 運彩量化決策（加密線上版）

運彩 +EV 分析工具：Elo 球隊實力＋先發投手近況模型、國際盤共識、凱利注碼、下注紀錄與績效統計。

> 🔒 **所有資料都經過加密**。儲存庫裡只有程式碼；歷史資料、模型、賽事與預測以 AES-256 加密存放，網站需輸入密碼才能解密檢視。

## 兩種使用方式

| | 線上版（GitHub Pages） | 電腦版 |
|---|---|---|
| 開啟方式 | 手機/電腦瀏覽器開網站網址 | 電腦執行 `啟動.bat` |
| 需要電腦開著 | ❌ 不需要 | ✅ 需要 |
| 資料更新 | GitHub 每 2 小時自動 | 電腦每 30 分鐘 |
| 中職先發 | 需自行輸入（官網只允許台灣連線） | 自動從中職官網同步 |
| 下注紀錄 | 存在各裝置瀏覽器（可匯出/匯入備份） | 存在電腦 |

## 線上版設定（只需一次）

在儲存庫 **Settings → Secrets and variables → Actions → New repository secret** 新增：

| 名稱 | 內容 | 必填 |
|---|---|---|
| `BM_PASSWORD` | 網站密碼（建議 12 字以上） | ✅ |
| `STATE_KEY` | 狀態加密金鑰（已自動設定，請勿修改） | ✅ |
| `ODDS_API_KEY` | [The Odds API](https://the-odds-api.com) 金鑰，提供國際盤賠率 | 選填 |

然後在 **Settings → Pages → Source** 選 **GitHub Actions**，到 **Actions → 更新資料並部署網站 → Run workflow** 執行一次。

## 檔案說明

- `cloud/build.py`：雲端更新流程（同步 → 訓練 → 加密輸出）
- `cloud/crypto.py`：PBKDF2 + AES-256-GCM 加密（與網頁 WebCrypto 相容）
- `docs/`：線上版網頁（解密與計算都在瀏覽器進行）
- `engine.py` / `model.py` / `pitching.py`：計算引擎、Elo 模型、先發投手模型
- `live_feed.py` / `cpbl_feed.py`：MLB、中職、國際盤資料同步
- `app.py` / `access.py` / `static/`：電腦版（含手機連線與 PIN 保護）

## 注意

本工具為統計分析用途，不保證獲利。運彩含莊家抽水，長期而言多數下注為負期望值；請量力而為，未滿 18 歲不得購買運動彩券。

公開儲存庫若 60 天沒有新的提交，GitHub 會暫停排程；屆時到 Actions 頁面按「Enable workflow」即可恢復。
