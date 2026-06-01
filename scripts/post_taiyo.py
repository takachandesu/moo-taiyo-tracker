#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中小型 × 新規(初回) 大量保有報告書 自動ツイート（1回の実行で最もインパクトのある1件のみ）

  - 取得・整形は taiyo_common.py（報告書ベース/方式A）に委譲
  - 中小型ユニバース × 新規(初回=350) × 未投稿 を候補に
  - "インパクト"優先で1件だけ選んでフィールド型ツイート（URLつき）
      * 当面はメタデータのみ → インパクト = 著名/アクティビスト系の提出者 + 新しい順
      * 保有割合の原本解析を足したら、比率/Δ比率ベースに切り替え
  - 選ばれなかった候補は tweeted に積まないので、後続の実行で順に拾われる
  - 原本PDFをロリポップに再ホストし、その公開URLをツイートに添付

  配置: scripts/post_taiyo.py（taiyo_common.py と同じ scripts/ に置く）
  入力: data/notable_filers.txt（著名提出者の名前キーワード, 部分一致, 1行1件・任意）
        data/tweeted.json（ツイート済みdocID, 自動生成・コミットバック）
"""
import os, sys, io, json, time
from ftplib import FTP

import tweepy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taiyo_common as tc

# ─────────────────────────────────────────────────────────────
# 設定
# ─────────────────────────────────────────────────────────────
EDINET_DOC_URL = "https://api.edinet-fsa.go.jp/api/v2/documents/{docid}"

TWEETED_JSON   = "data/tweeted.json"
NOTABLE_FILERS = "data/notable_filers.txt"
TWEETED_KEEP   = 5000

PER_RUN        = 1          # ★ 1回の実行でツイートする件数
NOTABLE_ONLY   = True       # ★ True=有名ファンドが出た時だけツイート(無ければskip) / False=有名優先で常に1件
HASHTAGS       = "#大量保有 #日本株"

# 原本URL: "rehost"=PDFを自ドメインに再ホスト / "ownpage"=自サイト一覧ページ
URL_MODE        = "rehost"
PDF_PUBLIC_BASE = "https://moo-stock-blog.com/edinet"
PDF_FTP_SUBDIR  = "edinet"
OWNPAGE_URL     = "https://moo-stock-blog.com/taiyo-hoyu/"

EDINET_API_KEY = os.environ.get("EDINET_API_KEY", "")
X_KEYS = {k: os.environ.get(k, "") for k in
          ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}
FTP_HOST = os.environ.get("LOLIPOP_FTP_HOST", "")
FTP_USER = os.environ.get("LOLIPOP_FTP_USER", "")
FTP_PASS = os.environ.get("LOLIPOP_FTP_PASSWORD", "")
FTP_PATH = os.environ.get("LOLIPOP_FTP_PATH", "")


def log(*a): print("[taiyo-tweet]", *a, flush=True)


# ─────────────────────────────────────────────────────────────
# 状態(ツイート済み) / 著名提出者
# ─────────────────────────────────────────────────────────────
def load_tweeted() -> list:
    if os.path.exists(TWEETED_JSON):
        try:
            with open(TWEETED_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_tweeted(ids: list):
    os.makedirs(os.path.dirname(TWEETED_JSON), exist_ok=True)
    with open(TWEETED_JSON, "w", encoding="utf-8") as f:
        json.dump(ids[-TWEETED_KEEP:], f, ensure_ascii=False, indent=0)


def load_notable() -> list:
    if os.path.exists(NOTABLE_FILERS):
        with open(NOTABLE_FILERS, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return []


# ─────────────────────────────────────────────────────────────
# インパクト・スコア(メタデータ版)
# ─────────────────────────────────────────────────────────────
def is_notable(r: dict, notable: list) -> bool:
    """提出者が有名ファンド(notable_filers.txt のいずれか)に部分一致するか。
    英語の大文字小文字は無視する(両辺を小文字化して比較)。日本語は影響なし。"""
    filer = (r.get("filer") or "").lower()
    return any(k.lower() in filer for k in notable)


def impact_score(r: dict, notable: list) -> tuple:
    """大きいほど優先。(著名提出者か, 提出時刻) のタプルで比較する。"""
    # 将来: r["ratio"] / abs(r["ratio"]-r["ratioPrev"]) を最優先キーに追加する
    return (1 if is_notable(r, notable) else 0, r.get("submit", ""))


# ─────────────────────────────────────────────────────────────
# 原本URL(再ホスト)
# ─────────────────────────────────────────────────────────────
def download_pdf(docid: str) -> bytes:
    import requests
    rr = requests.get(EDINET_DOC_URL.format(docid=docid),
                      params={"type": 2, "Subscription-Key": EDINET_API_KEY}, timeout=60)
    rr.raise_for_status()
    return rr.content


def rehost_pdf(docid: str) -> str:
    pdf = download_pdf(docid)
    ftp = FTP(FTP_HOST, timeout=60)
    ftp.login(FTP_USER, FTP_PASS)
    target = f"{FTP_PATH.rstrip('/')}/{PDF_FTP_SUBDIR}"
    try:
        ftp.mkd(target)
    except Exception:
        pass
    ftp.cwd(target)
    ftp.storbinary(f"STOR {docid}.pdf", io.BytesIO(pdf))
    ftp.quit()
    return f"{PDF_PUBLIC_BASE.rstrip('/')}/{docid}.pdf"


def make_url(docid: str) -> str:
    return OWNPAGE_URL if URL_MODE == "ownpage" else rehost_pdf(docid)


# ─────────────────────────────────────────────────────────────
# ツイート
# ─────────────────────────────────────────────────────────────
def x_client() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=X_KEYS["X_API_KEY"], consumer_secret=X_KEYS["X_API_SECRET"],
        access_token=X_KEYS["X_ACCESS_TOKEN"], access_token_secret=X_KEYS["X_ACCESS_SECRET"],
    )


def build_tweet(r: dict, url: str) -> str:
    name  = (r.get("name")  or "")[:24]
    filer = (r.get("filer") or "")[:40]
    submit = (r.get("submit") or "").replace("-", "/")
    return (f"【大量保有・新規】{r['code']} {name}\n"
            f"提出者：{filer}\n"
            f"{submit} 提出（5%超）\n"
            f"{url}\n"
            f"{HASHTAGS}")


# ─────────────────────────────────────────────────────────────
# メイン
# ─────────────────────────────────────────────────────────────
def main():
    missing = [k for k, v in {"EDINET_API_KEY": EDINET_API_KEY, **X_KEYS}.items() if not v]
    if URL_MODE == "rehost":
        missing += [k for k, v in {"LOLIPOP_FTP_HOST": FTP_HOST, "LOLIPOP_FTP_USER": FTP_USER,
                                   "LOLIPOP_FTP_PASSWORD": FTP_PASS}.items() if not v]
    if missing:
        sys.exit("環境変数が不足: " + ", ".join(missing))

    tweeted = load_tweeted()
    seen = set(tweeted)
    notable = load_notable()

    # 共通取得(報告書ベース・ユニバースタグ付き)
    reports = tc.get_today_reports()

    # 全銘柄 × 新規(初回) × 未投稿  (上場発行会社=証券コードに紐づくもの全て)
    cands = [r for r in reports if r["isNew"] and r["docID"] not in seen]
    log(f"候補(全銘柄×新規×未投稿): {len(cands)} 件")

    # 有名ファンドが出た時だけツイートするモード
    if NOTABLE_ONLY:
        if not notable:
            log("notable_filers.txt が空です。有名ファンド限定モードでは何もツイートしません")
            return
        cands = [r for r in cands if is_notable(r, notable)]
        log(f"うち有名ファンド: {len(cands)} 件")

    if not cands:
        return

    # 優先順に並べ、上位 PER_RUN 件のみ(有名限定モードでは新しい順)
    cands.sort(key=lambda r: impact_score(r, notable), reverse=True)
    picks = cands[:PER_RUN]

    client = x_client()
    posted = 0
    for r in picks:
        try:
            url = make_url(r["docID"])
            client.create_tweet(text=build_tweet(r, url))
            tweeted.append(r["docID"])
            posted += 1
            log(f"tweeted: {r['code']} {r['name']} ({r['docID']}) notable={'Y' if is_notable(r, notable) else 'N'}")
        except Exception as e:
            log(f"FAILED {r['docID']}: {e}")   # 失敗は積まない=次回再試行

    if posted:
        save_tweeted(tweeted)
    log(f"完了: {posted} 件ツイート（残り候補 {len(cands)-posted} 件は次回以降）")


if __name__ == "__main__":
    main()
