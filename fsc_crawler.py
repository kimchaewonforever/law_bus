
from __future__ import annotations

import argparse
import csv
import json
import re
import ssl
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter

BASE = "https://www.fsc.gov.tw/ch/"
LIST_URL = BASE + "home.jsp?id=3&parentpath=0&mcustomize="
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fsc-crawler/1.0)"}
PAGE_SIZE = 15


class _LenientX509Adapter(HTTPAdapter):
   

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.mount("https://", _LenientX509Adapter())
    return s


def fetch(session: requests.Session, url: str, data: dict | None = None,
          retries: int = 3, timeout: int = 30) -> str:
    
    for attempt in range(1, retries + 1):
        try:
            if data is None:
                r = session.get(url, timeout=timeout)
            else:
                r = session.post(url, data=data, timeout=timeout)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            if attempt == retries:
                raise
            wait = 2 ** attempt
            print(f"  請求失敗({e}),{wait} 秒後重試...", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def parse_list(html: str) -> tuple[list[dict], int]:

    soup = BeautifulSoup(html, "html.parser")
    items = []
    for li in soup.select("div.newslist li[role=row]"):
        a = li.select_one("span.title a")
        if not a:
            continue 
        href = urljoin(BASE, a["href"])
        m = re.search(r"dataserno=(\d+)", href)
        items.append({
            "dataserno": m.group(1) if m else "",
            "date": li.select_one("span.date").get_text(strip=True),
            "unit": li.select_one("span.unit").get_text(strip=True),
            "title": a.get("title") or a.get_text(strip=True),
            "url": href,
        })
    m = re.search(r"頁數\s*\d+\s*/\s*(\d+)", soup.get_text())
    total_pages = int(m.group(1)) if m else 1
    return items, total_pages


def parse_article(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.select_one("div.maincontent")
    if main is None:
        return {"content": "", "attachments": []}
    body = main.select_one("div.page-edit") or main
    for br in body.find_all("br"):
        br.replace_with("\n")
    text = body.get_text()
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t　]+", " ", ln).strip() for ln in text.splitlines()]
    content = "\n".join(ln for ln in lines if ln)

    attachments = []
    for a in main.select('a[href*="websitedowndoc"]'):
        attachments.append({
            "name": a.get_text(strip=True) or a.get("title", ""),
            "url": urljoin(BASE, a["href"]),
        })
    return {"content": content, "attachments": attachments}


def crawl(pages: int, delay: float) -> list[dict]:
    session = make_session()
    results: list[dict] = []
    page = 1
    total = None
    while True:
        print(f"抓取列表第 {page} 頁...", file=sys.stderr)
        html = fetch(session, LIST_URL, data={"page": page, "pagesize": PAGE_SIZE})
        items, total = parse_list(html)
        if not items:
            break
        for it in items:
            print(f"  內文: {it['title'][:40]}", file=sys.stderr)
            try:
                it.update(parse_article(fetch(session, it["url"])))
            except requests.RequestException as e:
                print(f"  失敗,略過: {e}", file=sys.stderr)
                it.update({"content": "", "attachments": [], "error": str(e)})
            results.append(it)
            time.sleep(delay)
        last = total if pages == 0 else min(pages, total)
        if page >= last:
            break
        page += 1
        time.sleep(delay)
    print(f"完成,共 {len(results)} 筆(網站總頁數 {total})", file=sys.stderr)
    return results


def save(rows: list[dict], path: str, fmt: str) -> None:
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
        return
    fields = ["dataserno", "date", "unit", "title", "url", "content", "attachments"]
    for r in rows:  
        fields += [k for k in r if k not in fields]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            row = dict(r)
            row["attachments"] = " | ".join(f"{a['name']} {a['url']}" for a in r.get("attachments", []))
            w.writerow(row)


def main() -> None:
    p = argparse.ArgumentParser(description="金管會法規資訊爬蟲")
    p.add_argument("--pages", type=int, default=1, help="要抓幾頁,0 = 全部")
    p.add_argument("--delay", type=float, default=1.0, help="每次請求間隔秒數")
    p.add_argument("--format", choices=["csv", "json"], default="csv")
    p.add_argument("--out", default=None, help="輸出檔名")
    args = p.parse_args()
    out = args.out or f"fsc_laws.{args.format}"
    rows = crawl(args.pages, args.delay)
    save(rows, out, args.format)
    print(f"已儲存到 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
