@echo off
chcp 65001 >nul
cd /d %~dp0
echo 從 Cloudflare 官方 GitHub 下載 cloudflared（遠端連線用，約 60MB）...
curl -L -o cloudflared.exe https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
if exist cloudflared.exe (
  echo.
  echo 下載完成！請回到 BallMetrics「參數設定 - 手機使用」：先設定 PIN，再按「開啟遠端連線」。
) else (
  echo 下載失敗，請確認網路後再試一次。
)
pause
