
from __future__ import annotations

import argparse
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

from fsc_crawler import fetch, make_session, parse_article, parse_list, save

ENTRY_URL = "https://www.fsc.gov.tw/ch/home.jsp?id=131&parentpath=0,2"
LIST_URL = "https://www.fsc.gov.tw/ch/home.jsp?id=131&parentpath=0%2C2&mcustomize="
PAGE_SIZE = 100  


FIELD_PATTERNS = {
    "doc_no": r"發文字號[:：]\s*(.+)",
    "penalized": r"受處分人[:：]\s*(.+)",
    "subject": r"主旨[:：]\s*(.+)",
}


def form_data(html: str) -> dict:
    
    form = BeautifulSoup(html, "html.parser").select_one("form#messageform")
    if form is None:
        return {}
    return {i["name"]: i.get("value", "") for i in form.select("input[name]")
            if i.get("type") not in ("button", "submit")}


def extract_fields(content: str) -> dict:
    out = {}
    for key, pat in FIELD_PATTERNS.items():
        m = re.search(pat, content)
        out[key] = m.group(1).strip() if m else ""
    m = re.search(r"(?:新臺幣|處)\s*(?:[(（]下同[)）])?\s*([\d,]+(?:\.\d+)?)\s*(萬|億)?\s*元",
                  out["subject"] or content)
    out["fine"] = f"{m.group(1)}{m.group(2) or ''}元" if m else ""
    return out


def crawl(pages: int, delay: float) -> list[dict]:
    session = make_session()
    form = form_data(fetch(session, ENTRY_URL))
    results: list[dict] = []
    page, total = 1, None
    while True:
        print(f"抓取列表第 {page} 頁...", file=sys.stderr)
        html = fetch(session, LIST_URL, data={**form, "page": page, "pagesize": PAGE_SIZE})
        items, total = parse_list(html)
        form = form_data(html) or form  
        if not items:
            break
        for it in items:
            print(f"  [{len(results) + 1}] {it['title'][:40]}", file=sys.stderr)
            try:
                it.update(parse_article(fetch(session, it["url"])))
            except requests.RequestException as e:
                print(f"  失敗,略過: {e}", file=sys.stderr)
                it.update({"content": "", "attachments": [], "error": str(e)})
            it.update(extract_fields(it["content"]))
            results.append(it)
            time.sleep(delay)
        last = total if pages == 0 else min(pages, total)
        if page >= last:
            break
        page += 1
        time.sleep(delay)
    print(f"完成,共 {len(results)} 筆(網站總頁數 {total})", file=sys.stderr)
    return results


def main() -> None:
    p = argparse.ArgumentParser(description="金管會裁罰案件爬蟲")
    p.add_argument("--pages", type=int, default=0, help="要抓幾頁(每頁 100 筆),0 = 全部")
    p.add_argument("--delay", type=float, default=1.0, help="每次請求間隔秒數")
    p.add_argument("--format", choices=["csv", "json"], default="json")
    p.add_argument("--out", default=None, help="輸出檔名")
    args = p.parse_args()
    out = args.out or f"fsc_penalties.{args.format}"
    rows = crawl(args.pages, args.delay)
    save(rows, out, args.format)
    print(f"已儲存到 {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
