from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from fsc_crawler import fetch, make_session, parse_article, parse_list
from fsc_penalty_crawler import extract_fields

BASE = "https://www.fsc.gov.tw/ch/home.jsp?"
SECTIONS = {
    "news": {"name": "新聞稿", "params": "id=2&parentpath=0", "out": "fsc_news.json"},
    "laws": {"name": "法規資訊", "params": "id=3&parentpath=0", "out": "fsc_laws.json"},
    "penalties": {"name": "裁罰案件", "params": "id=131&parentpath=0,2",
                  "out": "fsc_penalties.json", "extra": extract_fields},
}
PAGE_SIZE = 50
MAX_PAGES = 5  

HERE = Path(__file__).resolve().parent
log = logging.getLogger("fsc_monitor")


def form_data(html: str) -> dict:
    form = BeautifulSoup(html, "html.parser").select_one("form#messageform, form#lawform")
    if form is None:
        return {}
    return {i["name"]: i.get("value", "") for i in form.select("input[name]")
            if i.get("type") not in ("button", "submit")}


def key(row: dict) -> str:
    m = re.search(r"dtable=(\w+)", row["url"])
    return f"{m.group(1) if m else ''}:{row['dataserno']}"


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def find_new(session: requests.Session, section: dict, seen: set[str]) -> list[dict]:
    form = form_data(fetch(session, BASE + section["params"]))
    list_url = BASE + section["params"].replace(",", "%2C") + "&mcustomize="
    new: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        html = fetch(session, list_url, data={**form, "page": page, "pagesize": PAGE_SIZE})
        form = form_data(html) or form
        items, _ = parse_list(html)
        fresh = [it for it in items if key(it) not in seen]
        new += fresh
        if not seen or len(fresh) < len(items) or not items:
            break  
    return new


def check_once(section: dict, out: Path, delay: float) -> int:
    rows = load(out)
    seen = {key(r) for r in rows}
    session = make_session()
    new = find_new(session, section, seen)
    if not new:
        log.info("[%s] 沒有新消息", section["name"])
        return 0
    added = 0
    for it in new:
        log.info("[%s] 新消息: %s %s", section["name"], it["date"], it["title"])
        time.sleep(delay)
        try:
            it.update(parse_article(fetch(session, it["url"])))
        except requests.RequestException as e:
            log.warning("內文抓取失敗,下次再試: %s", e)
            continue 
        if "extra" in section:
            it.update(section["extra"](it["content"]))
        it["fetched_at"] = datetime.now().isoformat(timespec="seconds")
        rows.append(it)
        added += 1
    rows.sort(key=lambda r: (r["date"], r["dataserno"]), reverse=True)
    write(out, rows)
    log.info("[%s] 新增 %d 筆,共 %d 筆,已存到 %s", section["name"], added, len(rows), out.name)
    return added


TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$e = [Security.SecurityElement]
$xml = "<toast><visual><binding template='ToastGeneric'><text>$($e::Escape($env:TOAST_TITLE))</text><text>$($e::Escape($env:TOAST_BODY))</text></binding></visual></toast>"
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($doc))
"""


def notify(title: str, body: str) -> None:
    """跳出 Windows 右下角通知;失敗只記錄,不影響爬取結果。"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", TOAST_PS],
            env={**os.environ, "TOAST_TITLE": title, "TOAST_BODY": body},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True, timeout=30, check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("通知失敗: %s", e)


def main() -> None:
    p = argparse.ArgumentParser(description="金管會新消息監控")
    p.add_argument("--section", choices=[*SECTIONS, "all"], default="news",
                   help="要監控的區塊;all = 三個都檢查")
    p.add_argument("--interval", type=float, default=0,
                   help="每隔幾分鐘檢查一次;0 = 只檢查一次就結束")
    p.add_argument("--out", default=None,
                   help="存檔位置(只能搭配單一區塊;預設見上方說明)")
    p.add_argument("--delay", type=float, default=1.0, help="每次請求間隔秒數")
    p.add_argument("--no-notify", action="store_true", help="執行完不要跳出通知")
    args = p.parse_args()
    keys = list(SECTIONS) if args.section == "all" else [args.section]
    if args.out and len(keys) > 1:
        p.error("--out 不能和 --section all 一起使用")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr),
                  logging.FileHandler(HERE / "fsc_news_monitor.log", encoding="utf-8")],
    )
    while True:
        failed = False
        summary = []
        for key in keys:
            section = SECTIONS[key]
            out = Path(args.out) if args.out else HERE / section["out"]
            try:
                summary.append(f"{section['name']} {check_once(section, out, args.delay)} 筆")
            except Exception:
                log.exception("[%s] 檢查失敗", section["name"])
                summary.append(f"{section['name']} 檢查失敗")
                failed = True
        if not args.no_notify:
            title = "金管會監控:有區塊檢查失敗,請看 log" if failed else "金管會監控完成"
            notify(title, "新增:" + "、".join(summary))
        if not args.interval:
            sys.exit(1 if failed else 0)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
