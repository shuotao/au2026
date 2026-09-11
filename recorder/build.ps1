# 把 au2026rec 打包成 Windows 執行檔。
#
#   .\build.ps1              → dist\au2026rec\au2026rec.exe（資料夾版，啟動快、推薦）
#   .\build.ps1 -OneFile     → dist\au2026rec.exe（單檔版，好搬但每次啟動要解壓約 100 MB）
#
# 打包內容不含瀏覽器：程式用的是你電腦上已安裝的 Chrome（[browser] channel）。
# 執行檔旁邊要放 config.toml 與 my_schedule.csv；沒有 config.toml 時選單第 9 項可以產生。

param(
    [switch]$OneFile,
    [switch]$Clean
)

# 注意：不要設 $ErrorActionPreference = 'Stop'。Windows PowerShell 5.1 會把原生
# 指令（pip、pyinstaller）寫到 stderr 的正常訊息當成終止錯誤。改用 $LASTEXITCODE 判斷。
Set-Location $PSScriptRoot

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安裝 pyinstaller..." -ForegroundColor Yellow
    python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Host "pyinstaller 安裝失敗" -ForegroundColor Red; exit 1 }
}

if ($Clean) {
    Write-Host "清掉舊的 build / dist..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

$pyiArgs = @(
    '--name', 'au2026rec'
    '--noconfirm'
    '--console'
    # playwright 的 node driver 要整包帶著，否則 launch / attach 模式起不來
    '--collect-all', 'playwright'
    '--collect-all', 'obsws_python'
    # Windows 沒有內建時區資料庫，ZoneInfo 靠 tzdata
    '--collect-data', 'tzdata'
    # 設定範本（init 要用）與課程網址對照表
    '--add-data', 'config.example.toml;.'
)

if (Test-Path 'catalog.json') {
    $pyiArgs += @('--add-data', 'catalog.json;.')
} else {
    Write-Host "提醒：找不到 catalog.json，打包版將不含課程網址對照表" -ForegroundColor Yellow
}

if ($OneFile) { $pyiArgs += '--onefile' }
$pyiArgs += 'au2026rec\__main__.py'

Write-Host "開始打包..." -ForegroundColor Cyan
python -m PyInstaller @pyiArgs
if ($LASTEXITCODE -ne 0) { Write-Host "打包失敗（pyinstaller 回傳 $LASTEXITCODE）" -ForegroundColor Red; exit 1 }

$exe = if ($OneFile) { 'dist\au2026rec.exe' } else { 'dist\au2026rec\au2026rec.exe' }
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host "`n完成：$exe（$size MB）" -ForegroundColor Green
    Write-Host "點兩下會出現操作選單；也可以照舊下指令，例如：$exe plan"
} else {
    Write-Host "`n打包失敗，沒有產生 $exe" -ForegroundColor Red
    exit 1
}
