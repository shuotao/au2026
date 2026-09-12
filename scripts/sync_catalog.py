#!/usr/bin/env python3
"""把官方 AU 2026 議程目錄同步進本站各頁面的內嵌資料。

用法（在 repo 根目錄）：
    python3 scripts/sync_catalog.py --fetch     # 重新抓官方目錄，再同步
    python3 scripts/sync_catalog.py             # 用 src/catalog/ 的快照同步
    python3 scripts/sync_catalog.py --dry-run   # 只列出差異，不寫檔

資料來源：官方議程目錄背後的 RainFocus 公開搜尋 API（現場目錄＋數位目錄兩個 widget），
原始快照存在 src/catalog/（不進版控）。

原則：
- 時間、地點、課名、形式、線上／現場的歸屬，一律以官方目錄為準。
- 頁面上人工整理的內容（中文課名、主題、導讀、選課 99 的中文摘要…）只要場次還在就沿用，
  不會被覆寫；只有官方目錄新增、頁面上還沒有的場次，才用 au2026_classify 自動補主題等欄位。
- 同一堂課若「現場」與「數位」兩個目錄都有，就是兩邊都能參加：不再只歸成其中一邊。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from au2026_classify import ai_of, prac_of, topic_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "src" / "catalog"
NEW_ZH = Path(__file__).resolve().parent / "new_sessions_zh.json"

API = "https://attend.autodesk.com/api/search"
WIDGETS = {  # 官方目錄頁面內嵌的公開 widget 設定（window.widget.widgetId / apiToken）
    "inperson": ("ZXljpRXbHJCqE9w8875SttLAcXa3Qkgy", "x8ffC8442jjLb6TEBZzg4yZ7fUexEZYe"),
    "digital": ("0aVYLMHAIFFraJcbQPHJSYHrpztjK0so", "aKTLFC98YJnUpg8C5UmdOLQAySZveYg6"),
}
CATALOG_URL = "https://conferences.autodesk.com/flow/autodesk/au2026/sessioncatalog/page/{}/session/{}"

# 後勤／非課程項目：不進議程頁（選課 99 若有人挑了則照樣保留）
LOGISTICS = {"Activity", "Meal", "Reception", "Braindate", "Certification"}
FMT_NORMAL = {
    "Technical Deep Dive - Digital": "Technical Deep Dive",
    "Strategy Talk - Digital": "Strategy Talk",
    "Strategy Talk - Panel": "Strategy Talk",
    "Lunch and Learn": "Lunch & Learn",
}
DAYNAME = {"2026-09-15": "Sep 15", "2026-09-16": "Sep 16", "2026-09-17": "Sep 17", "2026-09-18": "Sep 18"}
AUDAY = {"2026-09-15": 1, "2026-09-16": 2, "2026-09-17": 3}
TAB_ORDER = ["All", "Featured", "Community", "Live-streamed", "On-demand", "Digital only"]


# ───────────────────────────── 抓取 ─────────────────────────────
def fetch() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for name, (wid, tok) in WIDGETS.items():
        items: list[dict] = []
        frm = 0
        while True:
            out = subprocess.run(
                ["curl", "-sS", "--fail", "-X", "POST", API,
                 "-H", f"rfWidgetId: {wid}", "-H", f"rfApiProfileId: {tok}",
                 "-H", "Content-Type: application/x-www-form-urlencoded",
                 "-H", "Origin: https://conferences.autodesk.com",
                 "-H", "Referer: https://conferences.autodesk.com/",
                 "--data", f"type=session&size=50&from={frm}"],
                capture_output=True, check=True).stdout
            d = json.loads(out)
            sec = d["sectionList"][0] if "sectionList" in d else d
            got = sec.get("items", [])
            items += got
            frm += len(got)
            if not got or frm >= sec["total"]:
                break
            time.sleep(0.3)
        (RAW / f"cat_{name}.json").write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        print(f"抓取 {name}：{len(items)} 筆")


# ───────────────────────────── 官方資料整理 ─────────────────────────────
def attr(x: dict, aid: str) -> list[str]:
    return [a["value"] for a in x.get("attributevalues", []) if a["attribute_id"] == aid]


def base_code(code: str) -> str:
    return code[:-2] if code.endswith("-D") else code


def clean_abstract(raw: str) -> str:
    """官方摘要是 HTML；轉成純文字（保留段落換行）。"""
    s = raw or ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>\s*<p[^>]*>", "\n\n", s, flags=re.I)
    s = re.sub(r"</?(p|div|ul|ol)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "\n• ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in s.split("\n")]
    s = "\n".join(lines)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def clean_title(s: str) -> str:
    return " ".join((s or "").replace("\u200b", "").replace("\ufeff", "").split())


def squash(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split())


def speakers(x: dict) -> str:
    seen: list[str] = []
    for p in x.get("participants", []):
        n = " ".join((p.get("fullName") or "").split())
        if n and n not in seen:
            seen.append(n)
    return "; ".join(seen)


def times(x: dict) -> list[dict]:
    out = []
    for t in x["times"]:
        out.append(dict(
            date=t["date"], startmin=int(t["startTimeMin"]), endmin=int(t["endTimeMin"]),
            start=t["startTimeFormatted"], end=t["endTimeFormatted"],
            room=" ".join((t.get("room") or "").split()), dayname=t.get("dayDisplayName", ""),
            dayfull=t.get("dateFormatted", ""),
        ))
    return out


def track_of(x: dict) -> str:
    """官方有時給兩個 Track；各頁的 Track 篩選是單值清單，取第一個（主 Track）。"""
    v = attr(x, "Track")
    return v[0] if v else ""


def duration_of(x: dict, t: dict | None = None) -> str:
    """Duration 偶有多值（例：30 min; 45 min），取與實際時段長度相符的那個。"""
    v = attr(x, "Duration")
    if len(v) > 1 and t:
        mins = t["endmin"] - t["startmin"]
        for d in v:
            h = re.search(r"(\d+)\s*hr", d)
            m = re.search(r"(\d+)\s*min", d)
            if (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0) == mins:
                return d
    return v[0] if v else ""


def tabs(x: dict) -> list[str]:
    v = set(attr(x, "Digitaltabs"))
    return [t for t in TAB_ORDER if t in v] + sorted(v - set(TAB_ORDER))


def load_catalog():
    ip = json.loads((RAW / "cat_inperson.json").read_text(encoding="utf-8"))
    dg = json.loads((RAW / "cat_digital.json").read_text(encoding="utf-8"))
    C: dict[str, dict] = defaultdict(lambda: {"ip": [], "dg": []})
    for x in ip:
        for t in times(x):
            C[base_code(x["code"])]["ip"].append(dict(rec=x, t=t, code=x["code"], url=CATALOG_URL.format("inperson", x["sessionID"])))
    for x in dg:
        tb = tabs(x)
        mode = "live" if "Live-streamed" in tb else "od"   # On-demand 與 Digital only 都是隨選
        for t in times(x):
            C[base_code(x["code"])]["dg"].append(dict(rec=x, t=t, code=x["code"], mode=mode, tabs=tb,
                                                      url=CATALOG_URL.format("digital", x["sessionID"])))
    for v in C.values():
        v["ip"].sort(key=lambda e: (e["t"]["date"], e["t"]["startmin"]))
        v["dg"].sort(key=lambda e: (e["t"]["date"], e["t"]["startmin"]))
    return dict(C), ip, dg


def is_placeholder(e: dict) -> bool:
    """數位目錄裡隨選場次常掛在 9/15 06:00 的佔位時段，不是實際播出時間。"""
    t = e["t"]
    return e.get("mode") == "od" and t["date"] == "2026-09-15" and t["startmin"] == 360


def primary(v: dict) -> dict:
    if v["ip"]:
        return v["ip"][0]
    real = [e for e in v["dg"] if not is_placeholder(e)]
    return (real or v["dg"])[0]


def norm_fmt(t: str, old: str | None = None) -> str:
    if t == "Activity" and old:
        return old
    return FMT_NORMAL.get(t, t)


def fmt12(m: int) -> str:
    h, mm = divmod(m, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {ap}"


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def taipei(date: str, m: int) -> datetime:
    return datetime.strptime(date, "%Y-%m-%d") + timedelta(minutes=m + 15 * 60)


def band_of(dt: datetime) -> str:
    h = dt.hour
    return "凌晨" if h < 5 else "清晨" if h < 9 else "白天" if h < 18 else "晚間"


def dayname(date: str) -> str:
    if date in DAYNAME:
        return DAYNAME[date]
    return "AGN Meetup" if date > "2026-09-18" else date


def alt_times(v: dict) -> str:
    """同一堂現場課排了兩個時段（重複開課的 Lab 等）時，列出其他時段。"""
    if len(v["ip"]) < 2:
        return ""
    return "、".join(f"{DAYNAME.get(e['t']['date'], e['t']['date'])} {fmt12(e['t']['startmin'])}" for e in v["ip"][1:])


# ───────────────────────────── 內嵌資料讀寫 ─────────────────────────────
class Page:
    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.orig = self.text

    def span(self, var: str):
        m = re.search(r"const\s+" + var + r"\s*=\s*", self.text)
        if not m:
            raise KeyError(f"{self.path.name}: const {var} not found")
        obj, end = json.JSONDecoder().raw_decode(self.text, m.end())
        return obj, m.end(), end

    def get(self, var: str):
        return self.span(var)[0]

    def put(self, var: str, obj, compact: bool | None = None):
        old, a, b = self.span(var)
        literal = self.text[a:b]
        if compact is None:
            compact = not re.search(r'":\s', literal[:400])
        s = dumps(obj, compact)
        self.text = self.text[:a] + s + self.text[b:]

    def save(self, dry: bool) -> bool:
        if self.text == self.orig:
            return False
        if not dry:
            self.path.write_text(self.text, encoding="utf-8")
        return True


def dumps(obj, compact: bool) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":") if compact else (", ", ": "))
    return s.replace("</", "<\\/")


# ───────────────────────────── 各頁面 ─────────────────────────────
class Sync:
    def __init__(self, dry: bool):
        self.dry = dry
        self.C, self.ip_raw, self.dg_raw = load_catalog()
        self.zh_new: dict[str, str] = json.loads(NEW_ZH.read_text(encoding="utf-8")) if NEW_ZH.exists() else {}
        self.report: list[str] = []
        self.planner = Page(ROOT / "planner.html")
        self.old_data = {d["code"]: d for d in self.planner.get("DATA")}

    def log(self, s: str) -> None:
        self.report.append(s)

    # 議程頁收錄範圍：課程類場次（排除後勤），或原本就收錄的
    def in_scope(self, code: str) -> bool:
        v = self.C[code]
        return norm_fmt(primary(v)["rec"]["type"]) not in LOGISTICS or code in self.old_data

    def scope_codes(self) -> list[str]:
        return [c for c in self.C if self.in_scope(c)]

    def title_of(self, v: dict) -> str:
        return clean_title(primary(v)["rec"]["title"])

    # ── planner.html：全議程 DATA ＋ 數位細節 HHD ──
    def planner_data(self) -> list[dict]:
        rows = []
        for code in self.scope_codes():
            v = self.C[code]
            p = primary(v)
            rec, t = p["rec"], p["t"]
            old = self.old_data.get(code)
            fmt = norm_fmt(rec["type"], old and old["fmt"])
            title = self.title_of(v)
            pills = attr(rec, "AIACatalogPills") or (attr(v["ip"][0]["rec"], "AIACatalogPills") if v["ip"] else [])
            aia = "HSW" if "AIA HSW" in pills else ("LU" if "AIA LU" in pills else "")
            ab = clean_abstract(rec.get("abstract", ""))
            row = dict(
                code=code, title=title, fmt=fmt,
                src="Digital" if v["dg"] else "In-person only",
                day=dayname(t["date"]), start=fmt12(t["startmin"]), end=fmt12(t["endmin"]),
                startmin=t["startmin"], aia=aia,
                topic=old["topic"] if old else topic_of(title, fmt, ab),
                ai=old["ai"] if old else ai_of(title, ab),
                ondemand=any(e["mode"] == "od" for e in v["dg"]),
                alsoip=bool(v["ip"]),
            )
            alt = alt_times(v)
            if alt:
                row["alt"] = alt
            rows.append(row)
        order = {c: i for i, c in enumerate(self.old_data)}
        dayidx = {d: i for i, d in enumerate(["Sep 15", "Sep 16", "Sep 17", "Sep 18", "AGN Meetup"])}
        rows.sort(key=lambda r: (dayidx.get(r["day"], 9), r["startmin"], order.get(r["code"], 10**6), r["code"]))
        return rows

    def hhd_entry(self, code: str, old: dict | None) -> dict:
        v = self.C[code]
        real = [e for e in v["dg"] if not is_placeholder(e)]
        e = (real or v["dg"])[0]
        x = e["rec"]
        new = dict(
            dc=x["code"], sp=speakers(x), ab=clean_abstract(x.get("abstract", "")),
            pr="; ".join(attr(x, "Product")), tr=track_of(x),
            ind="; ".join(attr(x, "Industry")), lp="; ".join(attr(x, "LearningPath")),
            dur=duration_of(x, e["t"]), mode="Live" if e["mode"] == "live" else "On-demand",
            feat=1 if "Featured" in e["tabs"] else 0, url=e["url"],
        )
        return keep_equivalent(old, new)

    def sync_planner(self):
        pg = self.planner
        rows = self.planner_data()
        old_codes, new_codes = set(self.old_data), {r["code"] for r in rows}
        self.log(f"planner DATA：{len(self.old_data)} → {len(rows)} 場（新增 {len(new_codes - old_codes)}、移除 {len(old_codes - new_codes)}）")
        changed = Counter()
        for r in rows:
            o = self.old_data.get(r["code"])
            if not o:
                continue
            for k in ("day", "start", "end", "src", "alsoip", "title", "fmt", "ondemand", "aia"):
                if o.get(k) != r.get(k):
                    changed[k] += 1
        self.log(f"  既有場次欄位更新：{dict(changed)}")
        self.planner_rows = rows
        pg.put("DATA", rows)

        hhd_old = pg.get("HHD")
        hhd = {}
        for r in rows:
            if self.C[r["code"]]["dg"]:
                hhd[r["code"]] = self.hhd_entry(r["code"], hhd_old.get(r["code"]))
        self.log(f"planner HHD（數位細節）：{len(hhd_old)} → {len(hhd)}")
        pg.put("HHD", hhd)
        self.sync_picks(pg)

    # ── 選課 99 ──
    def sync_picks(self, pg: Page):
        picks = pg.get("PICKS")
        out, seen = [], {}
        for p in picks:
            code = p["code"]
            b = base_code(code)
            if b not in self.C and code + "-P" in self.C:   # 5059 → 5059-P（官方改了代碼）
                self.log(f"選課 99：{code} 官方改代碼為 {code}-P")
                b = code + "-P"
            v = self.C.get(b)
            if not v:
                self.log(f"選課 99：{code} 已不在官方目錄，移除")
                continue
            # 現場場次：依原本挑的時段對到同一場（重複開課的課會有兩個時段）
            ipe = None
            if v["ip"]:
                ipe = next((e for e in v["ip"] if e["t"]["date"] == p["date"] and hhmm(e["t"]["startmin"]) == p["start"]), None) \
                    or next((e for e in v["ip"] if e["t"]["date"] == p["date"]), None) or v["ip"][0]
            dge = None
            if v["dg"]:
                real = [e for e in v["dg"] if not is_placeholder(e)]
                dge = (real or v["dg"])[0]
            main = ipe or dge
            t, rec = main["t"], main["rec"]
            key = (b, t["date"], t["startmin"])
            if key in seen:   # 同一堂課同時挑了現場版與數位版（例：BLD2228 與 BLD2228-D）→ 合併
                self.log(f"選課 99：{code} 與 {seen[key]} 是同一場，合併為一張卡")
                continue
            seen[key] = code
            q = dict(p)
            q["code"] = main["code"] if ipe else dge["code"]
            if q["code"] != code:
                self.log(f"選課 99：{code} → {q['code']}（{'現場也有這堂課' if ipe else '只剩線上'}）")
            q["title_en"] = clean_title(rec["title"])
            q["date"], q["day"] = t["date"], DAYNAME.get(t["date"], p["day"])
            q["start"], q["end"] = hhmm(t["startmin"]), hhmm(t["endmin"])
            q["tpe"] = taipei(t["date"], t["startmin"]).strftime("%Y-%m-%d %H:%M")
            q["inperson"] = bool(ipe)
            q["digital"] = bool(dge)
            q["dmode"] = dge["mode"] if dge else ""
            q["room"] = t["room"] if ipe else ""
            q["url"] = main["url"]
            q["url_dg"] = dge["url"] if (dge and ipe) else ""
            fm = attr(rec, "Format")
            if fm:
                q["formats"] = fm
                if q.get("format") not in fm:
                    q["format"] = fm[0]
            q["alt"] = "、".join(f"{DAYNAME.get(e['t']['date'], e['t']['date'])} {fmt12(e['t']['startmin'])}（{e['t']['room']}）"
                                for e in v["ip"] if e is not ipe) if ipe and len(v["ip"]) > 1 else ""
            for k in ("date", "start", "end", "room"):
                if p.get(k) != q.get(k) and not (k == "room" and not ipe):
                    self.log(f"選課 99：{q['code']} {k}：{p.get(k)} → {q.get(k)}")
            out.append(q)
        both = sum(1 for q in out if q["inperson"] and q["digital"])
        self.log(f"選課 99：{len(picks)} → {len(out)} 張卡；現場＋線上 {both}、現場限定 {sum(1 for q in out if q['inperson'] and not q['digital'])}、線上限定 {sum(1 for q in out if not q['inperson'])}")
        self.picks = out
        pg.put("PICKS", out)

    # ── onsite.html：現場參加者 ──
    def sync_onsite(self):
        pg = Page(ROOT / "onsite.html")
        old = {d["c"]: d for d in pg.get("DATA")}
        rows = []
        for r in self.planner_rows:
            v = self.C[r["code"]]
            if not v["ip"] or r["day"] not in ("Sep 15", "Sep 16", "Sep 17"):
                continue
            e = v["ip"][0]
            t, rec = e["t"], e["rec"]
            o = old.get(r["code"], {})
            dmode = ""
            if v["dg"]:
                dmode = "lv" if any(x["mode"] == "live" for x in v["dg"]) else "od"
            ab = clean_abstract(rec.get("abstract", ""))
            sp = speakers(rec)
            row = dict(
                c=r["code"], t=r["title"], fmt=r["fmt"], day=r["day"], s=r["start"], e=r["end"], sm=r["startmin"],
                tw=taipei(t["date"], t["startmin"]).strftime("%m/%d %H:%M"), topic=r["topic"], ai=r["ai"], aia=r["aia"],
                ip=not v["dg"], dmode=dmode, lab=r["fmt"] == "Hands-on Lab",
                sp=o.get("sp") if o.get("sp") and set(o["sp"].split("; ")) == set(sp.split("; ")) else sp,
                ab=o.get("ab") if o.get("ab") and squash(o["ab"]) == squash(ab) else ab,
                url=e["url"], rm=t["room"],
            )
            alt = alt_times(v)
            if alt:
                row["alt"] = alt
            rows.append(row)
        self.log(f"onsite DATA：{len(old)} → {len(rows)} 場（現場限定 {sum(r['ip'] for r in rows)}、線上直播 {sum(r['dmode']=='lv' for r in rows)}、線上隨選 {sum(r['dmode']=='od' for r in rows)}）")
        self.onsite_rows = rows
        pg.put("DATA", rows)
        pg.save(self.dry)
        self.pages_saved.append(pg)

    # ── planner-tw.html：台北時間 ──
    def sync_tw(self):
        pg = Page(ROOT / "planner-tw.html")
        old = {d["c"]: d for d in pg.get("DATA")}
        rows, sid = [], {}
        missing_zh = []
        for r in self.planner_rows:
            v = self.C[r["code"]]
            if v["dg"]:
                real = [e for e in v["dg"] if not is_placeholder(e)]
                e = v["ip"][0] if v["ip"] else (real or v["dg"])[0]
                dm = "lv" if any(x["mode"] == "live" for x in v["dg"]) else "od"
                sid[r["code"]] = "digital/session/" + (real or v["dg"])[0]["rec"]["sessionID"]
            else:
                e = v["ip"][0]
                dm = ""
                sid[r["code"]] = "inperson/session/" + e["rec"]["sessionID"]
            t = e["t"]
            st = taipei(t["date"], t["startmin"])
            en = taipei(t["date"], t["endmin"])
            m = st.hour * 60 + st.minute
            pos = m - 1380 if m >= 720 else m + 60
            o = old.get(r["code"])
            zh = (o or {}).get("zh") or self.zh_new.get(r["code"], "")
            if not zh:
                missing_zh.append(r["code"])
            if o and o["t"] != r["title"] and r["code"] in self.zh_new:
                zh = self.zh_new[r["code"]]   # 課名改了且有新譯名時才換
            rows.append(dict(
                c=r["code"], t=r["title"], fmt=r["fmt"], src="線上" if v["dg"] else "現場", topic=r["topic"],
                tw=st.strftime("%m/%d %H:%M"), twe=en.strftime("%H:%M"), band=band_of(st),
                vegas=f"{DAYNAME.get(t['date'], datetime.strptime(t['date'], '%Y-%m-%d').strftime('%b %-d'))} {fmt12(t['startmin'])}",
                dur=max(t["endmin"] - t["startmin"], 0),
                auday=AUDAY.get(t["date"], 0) if pos >= 0 else 0, min=m,
                prac=o["prac"] if o else prac_of(r["title"], r["fmt"]), zh=zh, dm=dm,
            ))
        self.log(f"planner-tw DATA：{len(old)} → {len(rows)}（線上 {sum(r['src']=='線上' for r in rows)}：直播 {sum(r['dm']=='lv' for r in rows)}／隨選 {sum(r['dm']=='od' for r in rows)}；現場限定 {sum(r['src']=='現場' for r in rows)}）")
        if missing_zh:
            self.log(f"  ⚠ 缺中文課名 {len(missing_zh)} 場：{' '.join(missing_zh)}")
        self.tw_rows = rows
        pg.put("DATA", rows)
        pg.put("SESSIONID", sid, compact=True)
        pg.save(self.dry)
        self.pages_saved.append(pg)

    # ── 數位目錄三件套：digital-guide、挑課工具、錄影器 catalog ──
    def digital_rows(self):
        rows = []
        for x in self.dg_raw:
            tb = tabs(x)
            for t in times(x):
                rows.append((x, t, tb))
        rows.sort(key=lambda r: (r[1]["date"], r[1]["startmin"], r[0]["code"]))
        return rows

    def sync_digital_guide(self):
        pg = Page(ROOT / "digital-guide.html")
        old = defaultdict(list)
        for d in pg.get("DATA"):
            old[d["c"]].append(d)
        rows = []
        for x, t, tb in self.digital_rows():
            live = "Live-streamed" in tb
            st = taipei(t["date"], t["startmin"])
            en = taipei(t["date"], t["endmin"])
            o = next((d for d in old.get(x["code"], []) if d["date"] == t["date"]), None) or (old.get(x["code"]) or [None])[0]
            ab = clean_abstract(x.get("abstract", ""))
            dayn = t["dayname"]
            weekday = dayn.split(" (")[0] if " (" in dayn else dayn
            new = dict(
                c=x["code"], t=clean_title(x["title"]), type=x["type"],
                track=track_of(x) or "（未分類）", ind="; ".join(attr(x, "Industry")) or "Other",
                pr="; ".join(attr(x, "Product")) or "None", lp="; ".join(attr(x, "LearningPath")),
                dur=duration_of(x, t), sp=speakers(x), ab=ab,
                url=CATALOG_URL.format("digital", x["sessionID"]), live=live, feat=1 if "Featured" in tb else 0,
                vegas=f"{weekday} {fmt12(t['startmin'])}", vday=dayn, date=t["date"],
                tw=st.strftime("%m/%d %H:%M"), twe=en.strftime("%H:%M"), twd=st.strftime("%m/%d"),
                band=dict(凌晨="凌晨 00–05", 清晨="清晨 05–09", 白天="白天 09–18", 晚間="晚間 18–24")[band_of(st)],
                # PDT = UTC−7 → UTC epoch 秒，排序用
                sortk=int((datetime.strptime(t["date"], "%Y-%m-%d")
                           + timedelta(minutes=t["startmin"] + 7 * 60) - datetime(1970, 1, 1)).total_seconds()),
                bulk=(not live) and t["date"] == "2026-09-15" and t["startmin"] == 360,
                ai=(o or {}).get("ai", ai_of(x["title"], ab)),
            )
            rows.append(keep_equivalent(o, new))
        self.log(f"digital-guide DATA：{sum(len(v) for v in old.values())} → {len(rows)} 筆（直播 {sum(r['live'] for r in rows)}／隨選 {sum(not r['live'] for r in rows)}）")
        self.guide_rows = rows
        pg.put("DATA", rows)
        pg.save(self.dry)
        self.pages_saved.append(pg)

    def sync_pick_tool(self):
        path = ROOT / "AU2026_挑課工具.html"
        text = path.read_text(encoding="utf-8")
        m = re.search(r'(<script id="data" type="application/json">)(.*?)(</script>)', text, re.S)
        old_rows = json.loads(m.group(2))
        old = defaultdict(list)
        for d in old_rows:
            old[d["code"]].append(d)
        rows = []
        for x, t, tb in self.digital_rows():
            o = next((d for d in old.get(x["code"], []) if d["date"] == t["date"]), None)
            new = dict(
                code=x["code"], title=clean_title(x["title"]), type=x["type"],
                track=track_of(x), industry="; ".join(attr(x, "Industry")),
                products="; ".join(attr(x, "Product")), duration=duration_of(x, t),
                day=t["dayname"], date=t["date"], start=t["start"], end=t["end"],
                mode="Live" if "Live-streamed" in tb else "On-demand",
                featured="Yes" if "Featured" in tb else "",
                tabs=", ".join(v for v in tb if v != "All"),
                learningPath="; ".join(attr(x, "LearningPath")), speakers=speakers(x),
                abstract=" ".join((x.get("abstract") or "").split()),
                url=CATALOG_URL.format("digital", x["sessionID"]),
            )
            rows.append(keep_equivalent(o, new))
        self.log(f"挑課工具 data：{len(old_rows)} → {len(rows)} 筆")
        self.pick_tool_rows = rows
        blob = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
        text = text[:m.start(2)] + blob + text[m.end(2):]
        if not self.dry:
            path.write_text(text, encoding="utf-8")

    def run(self):
        self.pages_saved: list[Page] = []
        self.sync_planner()
        self.planner.save(self.dry)
        self.sync_onsite()
        self.sync_tw()
        self.sync_digital_guide()
        self.sync_pick_tool()
        print("\n".join(self.report))


def keep_equivalent(old: dict | None, new: dict) -> dict:
    """欄位語意沒變就沿用舊值（講者順序、摘要空白差異不算變更），減少無意義的 diff。"""
    if not old:
        return new
    out = dict(new)
    for k, v in new.items():
        if k not in old:
            continue
        ov = old[k]
        if ov == v:
            continue
        if isinstance(v, str) and isinstance(ov, str):
            if k in ("sp", "speakers") and set(ov.split("; ")) == set(v.split("; ")):
                out[k] = ov
            elif k in ("ab", "abstract") and squash(ov) == squash(v):
                out[k] = ov
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="先重新抓官方目錄")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    Sync(a.dry_run).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
