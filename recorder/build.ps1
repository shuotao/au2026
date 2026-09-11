# 把 au2026rec 打包成 Windows 執行檔。
#
#   .\build.ps1              → dist\au2026rec\au2026rec.exe（資料夾版，啟動快、推薦）
#   .\build.ps1 -OneFile     → dist\au2026rec.exe（單檔版，好搬但每次啟動要解壓約 100 MB）
#
# 打包內容不含瀏覽器：程式用的是你電腦上已安裝的 Chrome（[browser] channel）。
# 執行檔旁邊要放 config.toml 與 my_schedule.csv；沒有的話點兩下 exe 選 1 走引導設定。

param(
    [switch]$OneFile,
    [switch]$Clean,
    [switch]$Zip      # 另外打包成 zip，給別人直接用、不用編譯
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
    # 只刪 build/：dist/ 由 PyInstaller 自己重建，先刪掉反而可能因為檔案還被
    # 佔用（剛跑過的 exe、開著的檔案總管）而讓後續打包失敗。
    Write-Host "清掉舊的 build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force build -ErrorAction SilentlyContinue
    if (Test-Path build) { Write-Host "  ! build 刪不掉（可能有檔案被佔用），繼續" -ForegroundColor Yellow }
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

    # 把「放在 exe 旁邊才有用」的檔案一起帶過去
    $outDir = Split-Path $exe -Parent
    foreach ($f in @('使用說明.md', 'catalog.json')) {
        if (Test-Path $f) { Copy-Item $f $outDir -Force }
    }

    Write-Host "`n這個資料夾還需要你自己放兩個檔案：" -ForegroundColor Cyan
    foreach ($f in @('config.toml', 'my_schedule.csv')) {
        $mark = if (Test-Path (Join-Path $outDir $f)) { '有' } else { '缺' }
        $colour = if ($mark -eq '有') { 'Green' } else { 'Yellow' }
        Write-Host ("  [{0}] {1}" -f $mark, $f) -ForegroundColor $colour
    }
    Write-Host "  config.toml    → 點兩下 exe 選 1 走引導設定會幫你產生"
    Write-Host "  my_schedule.csv → 從 AU2026 網站 My Schedule 匯出（見 使用說明.md 第二節）"
    if ($Zip -and -not $OneFile) {
        $version = (Select-String -Path 'au2026rec\__init__.py' -Pattern '__version__ = "(.+)"').Matches.Groups[1].Value
        $zipPath = "dist\au2026rec-$version-win64.zip"
        Remove-Item $zipPath -ErrorAction SilentlyContinue
        Compress-Archive -Path $outDir -DestinationPath $zipPath
        $zipSize = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
        Write-Host "`n發佈包：$zipPath（$zipSize MB）" -ForegroundColor Green
        Write-Host "  收到的人解壓縮後點兩下 au2026rec.exe，選 1 走引導設定即可，不需要安裝 Python。"
    }

    Write-Host "`n點兩下 exe 會出現操作選單；也可以下指令，例如：$exe plan"
} else {
    Write-Host "`n打包失敗，沒有產生 $exe" -ForegroundColor Red
    exit 1
}
