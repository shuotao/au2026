# au2026rec — AU2026 自動開課 + OBS 錄影

把你在 AU2026 選好的課表（CSV）丟進去，它會照時間自動把瀏覽器切到課程網頁、
叫 OBS 開始錄影，時間到停止、存檔、換下一場。整晚不用守著。

```
課表 CSV ──► 補課程網址 ──► 排錄影時間軸 ──► 到點：把瀏覽器導到課程頁
             (catalog.json)     (避開重疊)          └─► OBS 開錄 → 到點停錄 → 存檔
```

**分工很簡單：**

* **你負責**：把瀏覽器開好、登入 AU2026、擺在你要錄的那個螢幕上（多大、要不要
  全螢幕都隨你）；在 OBS 裡指定要錄哪個螢幕。
* **程式負責**：時間到了把那個瀏覽器切到該上的課，然後叫 OBS 開錄、到點停錄。

程式**不會**動你的視窗大小與位置。也因為 OBS 錄的是整個螢幕而不是瀏覽器內部，
播放鍵沒點到、自動化壞掉，畫面照樣錄得下來。

## ⚠️ 免責聲明

**本工具錄下的內容僅供使用者個人學習與課後複習之用。**

* AU 課程的影音、簡報與講義著作權屬 **Autodesk 及各場次講者所有**，本工具不取得、
  不主張任何權利。
* 錄下的檔案**不得**公開散布、上傳分享平台、轉載、翻譯後散布、販售，
  或用於任何商業、教學營利、公開播映用途。
* 使用者須自行確認擁有該場次的合法觀看資格（有效的 AU Pass / 註冊），並自行遵守
  Autodesk University 的使用條款、著作權法及所在地相關法令。
* **使用者對自己的使用行為負全部責任。** 任何違法、侵權或違反服務條款的使用，
  與本工具的開發者無關，開發者不承擔任何法律責任或連帶責任。
* 本工具依「現狀」提供，不附任何形式的擔保；因使用本工具造成的任何損失
  （包含但不限於錄影失敗、檔案損毀、帳號受限）開發者不負責。

如果你不同意以上任何一點，請不要使用本工具。

程式碼本身以 MIT 授權釋出（見 [LICENSE](LICENSE)）。**授權只涵蓋程式碼，
不涵蓋你用它錄下的內容** —— 那部分的權利義務見上面的免責聲明。

## 一、兩種用法：執行檔或 Python

**想點兩下就用** → 打包成執行檔：

```powershell
cd recorder
.\build.ps1              # 產生 dist\au2026rec\au2026rec.exe（資料夾約 146 MB）
.\build.ps1 -OneFile     # 或單一檔案版，好搬但每次啟動要解壓
```

把 `dist\au2026rec` 整個資料夾複製到你要的地方，**點兩下 `au2026rec.exe`** 就會出現
操作選單（檢查 OBS、試錄、看時間軸、開始錄影…），不用記指令。也可以照舊下指令：
`au2026rec.exe plan`。

執行檔會在**目前所在的資料夾**讀寫 `config.toml`、`my_schedule.csv`、`catalog.json`
與 `logs\`，所以把課表跟 exe 放在同一個資料夾最省事。設定範本與 173 筆課程網址
對照表已經包在裡面，選單第 9 項可以產生 `config.toml`。

執行檔**不含瀏覽器**：它用的是你電腦上已安裝的 Chrome。

**想用 Python 跑** →

## 二、裝東西

```powershell
cd recorder
pip install -e .
# 若沒裝 Chrome，改用 Playwright 自帶的 Chromium：
#   playwright install chromium   （並把 config.toml 的 channel 改成 "chromium"）
```

## 三、跑起來（七步）

```powershell
au2026rec init                            # 1. 產生 config.toml
au2026rec obs-doctor --enable-websocket --apply   # 2. 開 OBS WebSocket，密碼自動填入
au2026rec display                         # 3. 列出螢幕，挑一個要錄的
au2026rec display --use 3                 # 4. 建立錄課場景（螢幕擷取 + 桌面音訊）
au2026rec obs-test --record-seconds 8     # 5. 確認真的錄得出檔、有畫面有聲音
au2026rec catalog                         # 6. 建立 code → 課程網址 對照表
au2026rec login                           # 7. 開瀏覽器登入 AU2026、把視窗拖到那個螢幕
au2026rec plan                            #    看時間軸對不對
au2026rec run                             #    開始待機
```

第 2 步要先**關掉 OBS**（OBS 執行中改它的設定檔會在關閉時被覆寫，程式會擋下來）。
它會讀 OBS 自己的設定檔，把 WebSocket 埠號與密碼填進 `config.toml`，你不用手抄。

## 四、瀏覽器怎麼接（`[browser] mode`）

| 模式 | 怎麼運作 | 適用 |
|---|---|---|
| `launch`（預設） | 程式開一個它控制得到的瀏覽器，登入狀態存在 `browser-profile/`。第一次跑時你在那個視窗登入 AU2026、把它擺到要錄的螢幕；之後程式每次都導同一個視窗，不動它的大小位置。 | 一般情況 |
| `attach` | 接管你自己用 `--remote-debugging-port=9222` 開好的瀏覽器（Brave / Chrome / Edge 都行），導的是同一個分頁。 | 想沿用平常在用的瀏覽器與登入 |
| `open` | 只呼叫系統「開這個網址」，零自動化。每場開一個新分頁。 | 自動化都不想碰 |

`attach` 要這樣啟動瀏覽器（Chrome 136 之後**一定**要另外指定 `--user-data-dir`，
用預設 profile 會被拒絕）：

```powershell
brave.exe --remote-debugging-port=9222 --user-data-dir="$env:USERPROFILE\au2026-profile"
```

`launch` / `attach` 失敗時，會依 `fallback_to_open` 自動退回 `open`，
不讓一場課因為自動化壞掉就完全錄不到。

## 五、課表從哪來

AU2026 的 My Schedule 匯出格式，直接吃：

```csv
Status,Session Title,Session Code,Date,Start Time,End Time,Room

Scheduled,Day 1 Keynote,KEY1001-D,2026-09-15,09:00,10:30,Digital G
```

* 時間視為**太平洋時間**（`[schedule] source_timezone`），台灣時間自動換算。
* CSV 沒有課程網址，所以 `Session Code` 靠 `catalog.json` 補。
  `au2026rec catalog` 會從同專案的 `AU2026_挑課工具.html` 抽出 173 筆課程網址。
  課表自己有 `URL` 欄時優先用課表的。
* 也吃 `.ics`、分號分隔、`code/title/date/start/end` 這類自訂欄名，
  以及寫在同一格的時間區間（`9:00–11:15 AM`）。
* `Status` 只錄 `Scheduled` / `Registered` / `Confirmed`（見 `status_include`），
  候補的會跳過並提醒。

## 六、時間重疊怎麼處理

`[schedule] overlap_policy = "shift"`（預設）：

* **直播場次**時間鎖死不動 —— 錄不到就沒了，所以永遠優先佔位。
  兩場直播撞期時，晚開始的那場標 `×` 跳過，由你自己取捨。
* **On-demand** 依課表順序往後排隊，自動避開直播區塊。內容隨選隨看，挪動不影響。

```
  #   台灣時間              太平洋              長度 類型         課程
  1   09/16 00:00–01:30   09/15 09:00     93m  Live       KEY1001-D Day 1 Keynote
  2 → 09/16 01:33–02:33   09/15 10:33     63m  On-demand  MFG1914-D Accelerating ...
        └ 與其他場次重疊，往後挪 1 小時 3 分（On-demand 隨時可看，挪動不影響內容）
```

其他選項：`skip`（重疊就跳過）、`keep`（照課表硬幹，會互相打斷）。

## 七、OBS 這端

**錄哪個螢幕由你決定**：`au2026rec display` 列出 OBS 認得的螢幕，
`au2026rec display --use N` 會**新增**一個場景（預設「AU2026 錄課」），內含：

* **螢幕擷取** —— 你挑的那個螢幕，自動縮放填滿畫布
* **桌面音訊擷取**（`device_id = "default"`）—— 跟著系統預設輸出裝置，換耳機也不用改

不動你原本的場景。不想要了在 OBS 裡直接刪掉那個場景即可。

> **為什麼要另建一個場景**：原本的場景常綁著「應用程式音訊擷取 + 特定視窗」，
> 換頁換視窗就沒聲音。無人值守錄一整晚，桌面音訊 + 螢幕擷取最不會出事。
> 代價是會錄到 LINE 通知等系統音 —— 錄之前把會叫的東西靜音。

`au2026rec obs-doctor` 會讀 OBS 自己的設定檔並指出問題：WebSocket 沒開、
錄影資料夾不存在、用 mp4（斷電整檔壞）等。

檔名由 `[recording] filename_template` 決定，做法是暫時改 OBS 的
FilenameFormatting，**每次跑完會自動還原**，不會污染你平常手動錄影的命名。

## 八、驗過的東西（2026-09-10 於本機實測）

| 項目 | 結果 |
|---|---|
| OBS 連線 | OBS 32.1.2 / websocket 5.7.3 |
| 場景建立 | 螢幕擷取（PHL 1920x1080 @1920,0）+ 桌面音訊，1:1 填滿畫布 |
| 錄影開始/停止/檔名 | 通，指定檔名生效，跑完還原原本樣板 |
| 視訊 | h264 1920x1080 60fps，實際螢幕內容、清晰無黑邊 |
| 音訊 | aac 48kHz 立體聲；有音樂時 mean −39.9 dB、靜音時 −90 dB |
| 檔案大小 | 約 400 MB / 90 分鐘（C: 剩 187 GB，錄滿三天沒問題） |

音訊 mean −39.9 dB 偏小，那是系統音量的關係。正式錄之前把 Windows 音量開到
七、八成，或在 OBS 裡對「AU2026 桌面音訊」加一點增益。

另外用 video.js 做的假課程頁（Brightcove 用的就是 video.js）跑過兩場連續排程：
導頁、按播放、切場景、分場錄影、檔名、報告都正確，錄出的畫面確認是**會動的影片**
（159 / 221 個變動幀），音訊 mean −19.2 dB。

## 九、正式跑之前

播放鍵的選擇器是唯一沒辦法事先驗的一環（要有登入後的播放頁才看得到），
所以留了工具：

```powershell
au2026rec probe KEY1001-D                     # 印出頁面上所有可點元素
au2026rec test-record KEY1001-D --seconds 30  # 完整流程試錄 30 秒
```

`probe` 印出來後，把命中播放的那個寫進 `[browser] play_selectors`
（清單由上往下試，點到第一個看得見的就停）。找不到也只是不會自動播，
畫面照錄 —— 第一場人工盯一下就能把選擇器補齊。

## 十、無人值守注意事項

* **不要讓電腦睡著、螢幕不要關**：`powercfg /change standby-timeout-ac 0`、
  `powercfg /change monitor-timeout-ac 0`（螢幕擷取抓的是實際畫面）。
* 錄影期間別動那個螢幕上的視窗；其他螢幕照用。
* 瀏覽器視窗怎麼擺就怎麼錄 —— 開始前先自己確認一次那個螢幕的畫面是你要的樣子
  （`au2026rec obs-test --record-seconds 8` 錄一段來看最準）。
* 按一次 `Ctrl-C`：把當前這場收尾（停止錄影、存檔）後結束；按第二次立即中止。
* 中途重跑沒關係，已結束的場次自動略過（要強制重錄加 `--include-past`）。
* 只錄特定幾場：`au2026rec run --only KEY1001-D --only KEY1002-D`
* 紀錄在 `logs/au2026rec.log`；每場結果（含輸出檔路徑）在 `logs/sessions.csv`，
  錄完一場就即時寫入，中途斷電也留得住。

## 十一、指令一覽

| 指令 | 用途 |
|---|---|
| `init` | 產生 `config.toml` |
| `catalog` | 從挑課工具 HTML 建立 `catalog.json`（`--source` 可自訂來源） |
| `validate` | 檢查課表讀不讀得懂、網址齊不齊 |
| `plan` | 印出實際錄影時間軸（直播撞期時 exit code 2） |
| `display` | 列出螢幕；`--use N` 建立錄課場景並寫回設定 |
| `obs-doctor` | 讀 OBS 設定檔檢查問題；`--enable-websocket --apply` 一次搞定 |
| `obs-test` | 測連線、場景；`--record-seconds N` 實際錄一段並驗證檔案 |
| `login` | 開瀏覽器讓你登入、把視窗擺到要錄的螢幕 |
| `probe <code\|url>` | 列出頁面可點元素，用來補 `play_selectors` |
| `test-record <code>` | 拿真實課程跑完整流程但只錄幾十秒 |
| `run` | 照課表無人值守執行（`-y` 跳過確認與登入等待） |

## 十二、開發

```powershell
python -m unittest discover -s tests -v
```

48 個測試：時間/日期/時長解析、欄名對應、AU 官方 CSV 與 ICS、重疊排隊與直播撞期、
檔名清理、設定驗證與寫回、導頁模式與退回機制。

```
recorder/
  au2026rec/
    cli.py        指令列進出口
    config.py     TOML 設定、驗證、就地寫回
    schedule.py   CSV / ICS → Session（時區、各種時間格式）
    catalog.py    code → 課程網址 對照表
    plan.py       重疊處理與錄影時間軸
    browser.py    導頁：launch / attach / open 三種模式
    obs.py        obs-websocket 錄影控制
    obslocal.py   讀本機 OBS 設定檔（密碼、輸出路徑、profile）
    obsscene.py   建立錄課場景（螢幕擷取 + 桌面音訊）
    runner.py     等時間 → 導頁 → 開錄 → 停錄 → 下一場
  build.ps1       打包成 Windows 執行檔
  tests/
  config.example.toml
```

`browser-profile/` 內含 Autodesk 登入 cookie，已列入 `.gitignore`，別提交也別分享。
