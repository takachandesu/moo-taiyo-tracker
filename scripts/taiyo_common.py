#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
taiyo_common.py — 大量保有報告書 共通取得モジュール（報告書ベース / 方式A）

  役割:
    EDINET API v2 から当日(または指定日)の大量保有報告書を取得し、
    「1報告書 = 1レコード」(=1提出者の保有)の形に整えて返す共通土台。
    ウィジェット用JSON生成・自動ツイート・(将来)ヒートマップが、すべてここを使う。

  方式A(報告書ベース)の方針:
    - タイル/行は「発行会社 × 提出者(グループ)」単位。他の保有者は別レコード。
    - 1件の保有割合は本人+共同保有者の合算(その報告書の中の話)。無関係な他社は合算しない。
    - 同一提出者の初回+変更が窓内に複数あれば dedup_latest() で最新1件に集約できる。

  リポジトリ配置(想定):
    scripts/taiyo_common.py      ← 本ファイル
    data/edinetcode.csv          ← EDINETコードリスト(Shift-JIS/CP932)。手DLして配置・定期更新
    data/growth250_codes.txt     ← グロース250の証券コード(4桁) 1行1件 (任意)
    data/smallmid_codes.txt      ← 中小型ユニバースの証券コード(4桁) 1行1件

  単体実行(ドライラン): python scripts/taiyo_common.py
    → 当日分を取得し、ユニバース別の件数と一覧を表示するだけ(ツイート/FTPなし)
"""
import os, sys, csv
import datetime as dt
import requests

JST = dt.timezone(dt.timedelta(hours=9))
EDINET_LIST_URL = "https://api.edinet-fsa.go.jp/api/v2/documents.json"

DOCTYPE_NEW    = "350"   # 初回 大量保有報告書
DOCTYPE_CHANGE = "360"   # 変更報告書
TAIYO_TYPES    = {DOCTYPE_NEW, DOCTYPE_CHANGE}   # 訂正(別コード)は対象外

DEFAULT_EDINETCODE_CSV = "data/edinetcode.csv"


def now_jst() -> dt.datetime:
    return dt.datetime.now(JST)


# ─────────────────────────────────────────────────────────────
# 入力ファイルの読み込み
# ─────────────────────────────────────────────────────────────
def load_edinet_map(path: str = DEFAULT_EDINETCODE_CSV) -> dict:
    """EDINETコードリスト(CP932)から EDINETコード → (証券コード4桁, 会社名)。"""
    if not os.path.exists(path):
        sys.exit(f"EDINETコードリストがありません: {path} を配置してください(Shift-JIS)")
    mp = {}
    with open(path, encoding="cp932", errors="replace", newline="") as f:
        next(f, None)  # 1行目はダウンロード実行日時などのメタ行
        reader = csv.DictReader(f)
        ec_key = sec_key = name_key = None
        for col in reader.fieldnames or []:
            if "ＥＤＩＮＥＴコード" in col: ec_key = col
            elif "証券コード" in col:      sec_key = col
            elif "提出者名" in col:        name_key = col
        if not (ec_key and sec_key):
            sys.exit("EDINETコードリストのヘッダーを認識できませんでした")
        for row in reader:
            ec  = (row.get(ec_key)  or "").strip()
            sec = (row.get(sec_key) or "").strip()
            name = (row.get(name_key) or "").strip() if name_key else ""
            if ec and sec:
                mp[ec] = (sec[:4], name)   # 証券コードは5桁(末尾0)→4桁
    return mp


def load_codeset(path: str) -> set:
    """証券コード(4桁)の集合を読み込む。ファイルが無ければ空集合。"""
    s = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = line.strip()
                if c and not c.startswith("#"):
                    s.add(c[:4])
    return s


# ─────────────────────────────────────────────────────────────
# EDINET 取得
# ─────────────────────────────────────────────────────────────
def fetch_docs(date: str = None, api_key: str = None) -> list:
    """指定日(JST, 既定=当日)の提出書類一覧を取得。"""
    api_key = api_key or os.environ.get("EDINET_API_KEY", "")
    if not api_key:
        sys.exit("EDINET_API_KEY が未設定です")
    date = date or now_jst().strftime("%Y-%m-%d")
    r = requests.get(EDINET_LIST_URL,
                     params={"date": date, "type": 2, "Subscription-Key": api_key},
                     timeout=30)
    r.raise_for_status()
    return r.json().get("results", []) or []


# ─────────────────────────────────────────────────────────────
# 報告書ベース(A)へ整形
# ─────────────────────────────────────────────────────────────
def extract_reports(docs: list, ecmap: dict) -> list:
    """
    書類一覧 → 大量保有(350/360)のうち、発行会社が上場(=証券コードに紐づく)もののみ抽出。
    1報告書1レコード。保有割合(ratio)はメタデータに無いので None(原本解析で後付け)。
    """
    out = []
    for d in docs:
        tc = d.get("docTypeCode")
        if tc not in TAIYO_TYPES:
            continue
        issuer = d.get("issuerEdinetCode")
        info = ecmap.get(issuer) if issuer else None
        if not info:
            continue   # 発行会社が解決できない(非上場・コードリスト未掲載)→除外
        code, name = info
        out.append({
            "docID":  d["docID"],
            "code":   code,
            "name":   name,
            "filer":  d.get("filerName", ""),
            "isNew":  tc == DOCTYPE_NEW,        # True=初回 / False=変更
            "ratio":  None,                     # 任意: 保有割合(%)。原本XBRL解析時に付与
            "ratioPrev": None,                  # 任意: 変更前(%)
            "submit": d.get("submitDateTime", ""),
            "docUrl": None,                     # 任意: 原本リンク。ツイート/表示側で付与
        })
    return out


def tag_universe(reports: list, universes: dict) -> list:
    """各レコードに、属するユニバース名のリストを付与。例: ["growth250","smallmid"]"""
    for r in reports:
        r["universe"] = [k for k, s in universes.items() if r["code"] in s]
    return reports


def dedup_latest(reports: list) -> list:
    """
    複数営業日ぶんをためる窓表示用: 同一(提出者×発行会社)は最新の提出だけ残す。
    (初回の後に変更が出ていれば、変更=最新の保有状況を採用)
    """
    best = {}
    for r in reports:
        key = (r["filer"], r["code"])
        if key not in best or r["submit"] > best[key]["submit"]:
            best[key] = r
    return list(best.values())


# ─────────────────────────────────────────────────────────────
# 高水準ヘルパ: 当日の報告書ベースレコードを、ユニバースタグ付きで返す
# ─────────────────────────────────────────────────────────────
def get_today_reports(edinetcode_csv: str = DEFAULT_EDINETCODE_CSV,
                      growth250_path: str = "data/growth250_codes.txt",
                      smallmid_path:  str = "data/smallmid_codes.txt",
                      date: str = None) -> list:
    ecmap = load_edinet_map(edinetcode_csv)
    universes = {
        "growth250": load_codeset(growth250_path),
        "smallmid":  load_codeset(smallmid_path),
    }
    docs = fetch_docs(date=date)
    reports = extract_reports(docs, ecmap)
    return tag_universe(reports, universes)


# ─────────────────────────────────────────────────────────────
# ドライラン
# ─────────────────────────────────────────────────────────────
def _dryrun():
    reports = get_today_reports()
    g = [r for r in reports if "growth250" in r["universe"]]
    s = [r for r in reports if "smallmid" in r["universe"]]
    new_s = [r for r in s if r["isNew"]]
    print(f"=== {now_jst():%Y-%m-%d} 当日の大量保有(上場・報告書ベース) ===")
    print(f"全体 {len(reports)} 件 / グロース250 {len(g)} 件 / 中小型 {len(s)} 件 "
          f"(うち中小型・新規 {len(new_s)} 件)")
    for r in sorted(s, key=lambda x: x["submit"], reverse=True):
        kind = "新規" if r["isNew"] else "変更"
        print(f"  [{kind}] {r['code']} {r['name']:<16} | {r['filer']:<24} | "
              f"{r['submit']} | {r['docID']} | {r['universe']}")


if __name__ == "__main__":
    _dryrun()
