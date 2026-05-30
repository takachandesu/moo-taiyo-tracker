#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_widget_data.py
  中小型(スタンダード+グロース全銘柄) の「新規取得(初回=350)・直近3営業日」を集計し、
  taiyo-widget.html が読む taiyo-data.json を生成 → ロリポップにFTPアップロード。

  - 取得/整形は taiyo_common.py(報告書ベース/方式A)に委譲
  - 直近3営業日(土日を除く)ぶんの書類一覧を取得して結合
  - 新規取得のみ(変更=360は載せない)
  - smallmid パネルが本命。growth250 パネルも同ルールで埋める(タブが空にならないように)
  - JSON は単一ファイルを上書きアップロード(同期型ではないので衝突なし)

  配置: scripts/build_widget_data.py (taiyo_common.py と同じ scripts/ に)
  必要: EDINET_API_KEY, LOLIPOP_FTP_HOST/USER/PASSWORD (Secrets)
        data/edinetcode.csv, data/smallmid_codes.txt, (任意)data/growth250_codes.txt
"""
import os, sys, io, json
import datetime as dt
from ftplib import FTP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taiyo_common as tc

# ─────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────
LOOKBACK_BIZDAYS = 3        # 直近何営業日ぶんを載せるか
NEW_ONLY         = True     # True=新規取得(初回)のみ / False=変更も含める
OUT_JSON_LOCAL   = "taiyo-data.json"

# widget の CONFIG.dataUrl と一致させること:
#   /wp-content/uploads/heatmaps/taiyo-data.json
# 下はFTPログイン直下からの相対パス。ロリポップのディレクトリ構成に合わせて調整。
DATA_REMOTE_DIR  = "wp-content/uploads/heatmaps"
DATA_REMOTE_NAME = "taiyo-data.json"

FTP_HOST = os.environ.get("LOLIPOP_FTP_HOST", "")
FTP_USER = os.environ.get("LOLIPOP_FTP_USER", "")
FTP_PASS = os.environ.get("LOLIPOP_FTP_PASSWORD", "")


def log(*a): print("[taiyo-data]", *a, flush=True)


def last_bizdays(n: int) -> list:
    """今日(JST)から遡って、土日を除く直近 n 日の 'YYYY-MM-DD' を返す。"""
    out, d = [], tc.now_jst().date()
    while len(out) < n:
        if d.weekday() < 5:          # 0=月 .. 4=金
            out.append(d.strftime("%Y-%m-%d"))
        d -= dt.timedelta(days=1)
    return out


def slim(r: dict) -> dict:
    """widget が使うフィールドだけに整形。"""
    return {
        "code": r["code"], "name": r["name"], "filer": r["filer"],
        "isNew": r["isNew"], "ratio": r.get("ratio"), "ratioPrev": r.get("ratioPrev"),
        "submit": r["submit"], "docUrl": r.get("docUrl"),
    }


def main():
    missing = [k for k, v in {"EDINET_API_KEY": os.environ.get("EDINET_API_KEY", ""),
                              "LOLIPOP_FTP_HOST": FTP_HOST, "LOLIPOP_FTP_USER": FTP_USER,
                              "LOLIPOP_FTP_PASSWORD": FTP_PASS}.items() if not v]
    if missing:
        sys.exit("環境変数が不足: " + ", ".join(missing))

    ecmap = tc.load_edinet_map()
    universes = {
        "growth250": tc.load_codeset("data/growth250_codes.txt"),
        "smallmid":  tc.load_codeset("data/smallmid_codes.txt"),
    }

    # 直近3営業日ぶんを取得して結合
    all_reports = []
    dates = last_bizdays(LOOKBACK_BIZDAYS)
    for date in dates:
        docs = tc.fetch_docs(date=date)
        reps = tc.tag_universe(tc.extract_reports(docs, ecmap), universes)
        if NEW_ONLY:
            reps = [r for r in reps if r["isNew"]]
        all_reports.extend(reps)
        log(f"{date}: {len(reps)} 件(新規取得)")

    def panel(key):
        rs = [slim(r) for r in all_reports if key in r["universe"]]
        rs.sort(key=lambda x: x["submit"], reverse=True)
        return {"reports": rs}

    payload = {
        "updated": tc.now_jst().strftime("%Y-%m-%d %H:%M"),
        "lookbackDays": LOOKBACK_BIZDAYS,
        "panels": {"growth250": panel("growth250"), "smallmid": panel("smallmid")},
    }

    with open(OUT_JSON_LOCAL, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    g = len(payload["panels"]["growth250"]["reports"])
    s = len(payload["panels"]["smallmid"]["reports"])
    log(f"生成: グロース250 {g} 件 / 中小型 {s} 件 (直近{LOOKBACK_BIZDAYS}営業日・新規取得)")

    # FTPアップロード(単一ファイル上書き)
    with open(OUT_JSON_LOCAL, "rb") as f:
        data = f.read()
    ftp = FTP(FTP_HOST, timeout=60)
    ftp.login(FTP_USER, FTP_PASS)
    for part in DATA_REMOTE_DIR.strip("/").split("/"):   # 1階層ずつ移動(無ければ作る)
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
