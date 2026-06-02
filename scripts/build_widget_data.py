#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_widget_data.py  (全銘柄対応版)
  「全銘柄の新規取得(初回=350)・直近3営業日」を集計し、taiyo-widget.html が読む
  taiyo-data.json を生成 → ロリポップにFTPアップロード。

  パネル構成(全銘柄方針):
    all     : 全銘柄の新規取得(直近3営業日)
    notable : うち data/notable_filers.txt の有名投資家によるもの(部分一致・大小無視)

  ※ 銘柄コードの絞り込みリスト(smallmid_codes.txt 等)は不要。
     上場発行会社(=証券コードに紐づくもの)はすべて対象。
"""
import os, sys, io, json
import datetime as dt
from ftplib import FTP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taiyo_common as tc

LOOKBACK_BIZDAYS = 3
NEW_ONLY         = True
NOTABLE_FILERS   = "data/notable_filers.txt"
OUT_JSON_LOCAL   = "taiyo-data.json"

# widget の CONFIG.dataUrl と一致させる: /wp-content/uploads/heatmaps/taiyo-data.json
# ★ロリポップはログイン直後がルート(moo-stock-blog と wp-content が並ぶ場所)。
#   サイト本体は moo-stock-blog フォルダの中なので、その配下を指定する。
DATA_REMOTE_DIR  = "moo-stock-blog/wp-content/uploads/heatmaps"
DATA_REMOTE_NAME = "taiyo-data.json"

FTP_HOST = os.environ.get("LOLIPOP_FTP_HOST", "")
FTP_USER = os.environ.get("LOLIPOP_FTP_USER", "")
FTP_PASS = os.environ.get("LOLIPOP_FTP_PASSWORD", "")


def log(*a): print("[taiyo-data]", *a, flush=True)


def last_bizdays(n: int) -> list:
    out, d = [], tc.now_jst().date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= dt.timedelta(days=1)
    return out


def load_notable() -> list:
    if os.path.exists(NOTABLE_FILERS):
        with open(NOTABLE_FILERS, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return []


def is_notable(r: dict, notable: list) -> bool:
    filer = (r.get("filer") or "").lower()
    return any(k.lower() in filer for k in notable)


def slim(r: dict) -> dict:
    return {
        "docID": r["docID"],
        "code": r["code"], "name": r["name"], "filer": r["filer"],
        "isNew": r["isNew"], "ratio": r.get("ratio"), "ratioPrev": r.get("ratioPrev"),
        "purpose": r.get("purpose"),
        "submit": r["submit"], "docUrl": r.get("docUrl"),
    }


def main():
    missing = [k for k, v in {"EDINET_API_KEY": os.environ.get("EDINET_API_KEY", ""),
                              "LOLIPOP_FTP_HOST": FTP_HOST, "LOLIPOP_FTP_USER": FTP_USER,
                              "LOLIPOP_FTP_PASSWORD": FTP_PASS}.items() if not v]
    if missing:
        sys.exit("環境変数が不足: " + ", ".join(missing))

    ecmap = tc.load_edinet_map()
    notable = load_notable()

    # 直近3営業日ぶんを取得・結合(全銘柄=上場発行会社すべて)
    all_reports = []
    for date in last_bizdays(LOOKBACK_BIZDAYS):
        docs = tc.fetch_docs(date=date)
        reps = tc.extract_reports(docs, ecmap)
        if NEW_ONLY:
            reps = [r for r in reps if r["isNew"]]
        all_reports.extend(reps)
        log(f"{date}: {len(reps)} 件(新規取得)")

    # 原本(CSV)から保有割合を全件付与(1件1リクエスト。EDINETに優しく0.2秒間隔)
    log(f"原本から保有割合を取得中… {len(all_reports)} 件")
    tc.enrich_with_ratio(all_reports, sleep=0.2)
    got = sum(1 for r in all_reports if r.get("ratio") is not None)
    log(f"保有割合 取得済み: {got}/{len(all_reports)} 件")

    def panel(reports):
        rs = [slim(r) for r in reports]
        rs.sort(key=lambda x: x["submit"], reverse=True)
        return {"reports": rs}

    notable_reports = [r for r in all_reports if is_notable(r, notable)]

    payload = {
        "updated": tc.now_jst().strftime("%Y-%m-%d %H:%M"),
        "lookbackDays": LOOKBACK_BIZDAYS,
        "panels": {"all": panel(all_reports), "notable": panel(notable_reports)},
    }

    with open(OUT_JSON_LOCAL, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    log(f"生成: 全銘柄 {len(all_reports)} 件 / 注目投資家 {len(notable_reports)} 件 "
        f"(直近{LOOKBACK_BIZDAYS}営業日・新規取得)")

    with open(OUT_JSON_LOCAL, "rb") as f:
        data = f.read()
    ftp = FTP(FTP_HOST, timeout=60)
    ftp.login(FTP_USER, FTP_PASS)
    for part in DATA_REMOTE_DIR.strip("/").split("/"):
        try:
            ftp.mkd(part)
        except Exception:
            pass
        ftp.cwd(part)
    ftp.storbinary(f"STOR {DATA_REMOTE_NAME}", io.BytesIO(data))
    ftp.quit()
    log("アップロード完了")


if __name__ == "__main__":
    main()
