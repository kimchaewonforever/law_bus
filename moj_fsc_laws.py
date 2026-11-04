
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from fsc_crawler import fetch, make_session

SITE = "https://law.moj.gov.tw"
LIST_URL = SITE + "/Law/LawSearchLaw.aspx?TY={ty}"
LAW_URL = SITE + "/LawClass/LawAll.aspx?pcode={pcode}"
FSC_TY = "04018"  
ABOLISHED_TY = FSC_TY + "000&fei=1"

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "法規"


def roc_to_iso(text: str) -> str:
    
    m = re.search(r"民國\s*(\d+)\s*年\s*(\d+)\s*月\s*(\d+)\s*日", text or "")
    if not m:
        return ""
    y, mo, d = map(int, m.groups())
    return f"{y + 1911:04d}-{mo:02d}-{d:02d}"


def safe_name(name: str) -> str:
    
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip()[:150]


def list_categories(session: requests.Session) -> dict[str, str]:
    
    html = fetch(session, LIST_URL.format(ty=FSC_TY + "001"))
    found = re.findall(rf'TY=({FSC_TY}\d{{3}})">([^<]+)<span', html)
    return {ty: name.strip() for ty, name in found if not ty.endswith("000")}


def list_laws(session: requests.Session, ty: str) -> list[dict]:
    soup = BeautifulSoup(fetch(session, LIST_URL.format(ty=ty)), "html.parser")
    laws = []
    for a in soup.select("a#hlkLawName"):
        m = re.search(r"PCode=(\w+)", a["href"], re.I)
        if not m:
            continue
        laws.append({
            "pcode": m.group(1),
            "name": a.get("title") or a.get_text(strip=True),
            "list_date": roc_to_iso(a.parent.get_text(" ", strip=True)),
        })
    return laws


def parse_law(html: str, pcode: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, str] = {}
    attachments = []
    for tr in soup.select("div.table-title table tr"):
        th, td = tr.find("th"), tr.find("td")
        if not th or not td:
            continue
        key = th.get_text(strip=True).rstrip("：:")
        if key == "附檔":
            for a in td.select("a[href]"):
                attachments.append({"name": a.get_text(strip=True),
                                    "url": SITE + "/LawClass/" + a["href"]})
        else:
            meta[key] = re.sub(r"\s+", " ", td.get_text(" ", strip=True))

    articles = []
    chapter = ""
    content = soup.select_one("div.law-reg-content")
    for el in content.select("div.h3, div.row") if content else []:
        if "h3" in el.get("class", []):
            chapter = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            continue
        no = el.select_one("div.col-no a")
        body = el.select_one("div.col-data")
        if not no or not body:
            continue
        lines = [d.get_text(strip=True) for d in body.select("div[class^=line-]")]
        articles.append({
            "chapter": chapter,
            "article_no": re.sub(r"\s+", " ", no.get_text(strip=True)),
            "content": "\n".join(lines) if lines else body.get_text("\n", strip=True),
            "has_attachment": el.select_one("i.fa-paperclip") is not None,
        })

    name_link = soup.select_one("a#hlLawName")  
    if name_link:
        meta["法規名稱"] = name_link.get_text(strip=True)
    amended = meta.get("修正日期", "")
    published = meta.get("發布日期") or meta.get("公布日期", "")
    return {
        "pcode": pcode,
        "name": meta.get("法規名稱", ""),
        "category": meta.get("法規類別", ""),
        "amended_date": roc_to_iso(amended),
        "published_date": roc_to_iso(published),
        "status": meta.get("生效狀態", ""),
        "url": LAW_URL.format(pcode=pcode),
        "attachments": attachments,
        "article_count": len(articles),
        "articles": articles,
        "meta": meta,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_index() -> dict[str, dict]:
    path = OUT_DIR / "_index.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {r["pcode"]: r for r in json.load(f)}


def crawl(categories: dict[str, str], delay: float, force: bool) -> None:
    session = make_session()
    index = load_index()
    done = failed = skipped = 0
    for ty, cat in categories.items():
        laws = list_laws(session, ty)
        print(f"[{cat}] {len(laws)} 部法規", file=sys.stderr)
        time.sleep(delay)
        for law in laws:
            pcode = law["pcode"]
            path = OUT_DIR / safe_name(cat) / f"{safe_name(law['name'])}.json"
            old = index.get(pcode)
            if (not force and old and old["list_date"] == law["list_date"]
                    and (HERE / old["file"]).exists()):
                skipped += 1
                continue
            print(f"  {law['name']}", file=sys.stderr)
            try:
                data = parse_law(fetch(session, LAW_URL.format(pcode=pcode)), pcode)
            except requests.RequestException as e:
                print(f"  失敗,略過: {e}", file=sys.stderr)
                failed += 1
                continue
            data["name"] = data["name"] or law["name"]
            write_json(path, data)
            index[pcode] = {
                "pcode": pcode, "name": data["name"], "category": cat,
                "list_date": law["list_date"], "amended_date": data["amended_date"],
                "article_count": data["article_count"],
                "file": path.relative_to(HERE).as_posix(),
            }
            write_json(OUT_DIR / "_index.json", sorted(index.values(), key=lambda r: r["pcode"]))
            done += 1
            time.sleep(delay)
    print(f"完成:新抓/更新 {done} 部、未變動跳過 {skipped} 部、失敗 {failed} 部"
          f",存在 {OUT_DIR}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description="全國法規資料庫 金管會法規爬蟲")
    p.add_argument("--category", action="append",
                   help="只抓指定分類的 TY 代碼,可重複,例如 04018008(銀行目)")
    p.add_argument("--include-abolished", action="store_true", help="也抓已廢止的法規")
    p.add_argument("--force", action="store_true", help="不論有沒有修正都重新抓")
    p.add_argument("--delay", type=float, default=1.0, help="每次請求間隔秒數")
    args = p.parse_args()

    categories = list_categories(make_session())
    if args.category:
        categories = {ty: n for ty, n in categories.items() if ty in args.category}
    if args.include_abolished:
        categories[ABOLISHED_TY] = "已廢止"
    if not categories:
        p.error("找不到指定的分類")
    crawl(categories, args.delay, args.force)


if __name__ == "__main__":
    main()
