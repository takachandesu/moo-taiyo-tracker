#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_xbrl.py  — 原本構造の調査用（使い捨て）
  当日(または直近営業日)の大量保有報告書(350)を1件取り、原本ZIPの中身を
  ログにダンプする。保有割合がどのファイル・どの項目に入っているか特定するため。

  EDINETの原本は type で種類が違う:
    type=1: 提出本文・監査報告書(XBRL一式)
    type=5: CSV(取込用。UTF-16/TAB区切り。人間が一番読みやすい)
  まず type=5(CSV) を試し、無ければ type=1(XBRL) を覗く。

  GitHub Actions 専用(EDINET_API_KEY を使う)。一度結果を見たら捨ててよい。
"""
import os, sys, io, zipfile
import datetime as dt
import requests

JST = dt.timezone(dt.timedelta(hours=9))
KEY = os.environ.get("EDINET_API_KEY", "")
LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
DOC_URL  = "https://api.edinet-fsa.go.jp/api/v2/documents/{docid}"


def log(*a): print(*a, flush=True)


def find_one_350():
    """直近の営業日をさかのぼり、初回大量保有報告書(350)を1件見つける。"""
    d = dt.datetime.now(JST).date()
    for _ in range(10):
        if d.weekday() < 5:
            date = d.strftime("%Y-%m-%d")
            r = requests.get(LIST_URL, params={"date": date, "type": 2,
                             "Subscription-Key": KEY}, timeout=30)
            if r.ok:
                for doc in r.json().get("results", []) or []:
                    if doc.get("docTypeCode") == "350":
                        log(f"=== 対象: {date} docID={doc['docID']} "
                            f"提出者={doc.get('filerName')} 発行会社EC={doc.get('issuerEdinetCode')}")
                        return doc["docID"]
        d -= dt.timedelta(days=1)
    sys.exit("直近10営業日に初回大量保有報告書が見つかりませんでした")


def dump_zip(docid, doc_type, label):
    log(f"\n########## type={doc_type} ({label}) ##########")
    r = requests.get(DOC_URL.format(docid=docid),
                     params={"type": doc_type, "Subscription-Key": KEY}, timeout=60)
    log(f"HTTP {r.status_code} / Content-Type={r.headers.get('Content-Type')} / {len(r.content)} bytes")
    if not r.ok:
        log("取得失敗"); return
    try:
        z = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception as e:
        log(f"ZIPではない: {e}"); return
    log("---- ZIP内のファイル一覧 ----")
    for n in z.namelist():
        log("  ", n)
    # CSVらしきものを1つ、中身を表示
    for n in z.namelist():
        if n.lower().endswith(".csv"):
            raw = z.read(n)
            text = None
            for enc in ("utf-16", "utf-16-le", "cp932", "utf-8"):
                try:
                    text = raw.decode(enc); used = enc; break
                except Exception:
                    continue
            log(f"\n---- {n} (decoded as {used}) 先頭60行 ----")
            for i, line in enumerate(text.splitlines()[:60]):
                log(f"{i:3}| {line[:200]}")
            # 「割合」を含む行だけ抜き出す
            log(f"\n---- {n} 内で『割合』を含む行 ----")
            for line in text.splitlines():
                if "割合" in line or "保有" in line:
                    log("  ", line[:200])
            break


def main():
    if not KEY:
        sys.exit("EDINET_API_KEY が未設定")
    docid = find_one_350()
    dump_zip(docid, 5, "CSV取込用")
    dump_zip(docid, 1, "XBRL本文")


if __name__ == "__main__":
    main()
