#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
post_daily.py — 1日1回・夕方の確定版ブログ記事を投稿し、注目投資家がいればツイート

  フロー:
    1. その日(JST)提出ぶんの大量保有報告書(新規350)を取得・保有割合付与
    2. WordPress REST API で記事を1本投稿（その日まだ無ければ。重複防止）
    3. 注目投資家(notable_filers.txt)の新規があれば、記事URL付きで1回ツイート
       （その日まだツイートしていなければ。重複防止。文字数に収まるだけ詰める）

  想定スケジュール: JST 18:00 頃に1回（提出受付17:15終了後の確定版）
  配置: scripts/post_daily.py
  Secrets: EDINET_API_KEY / WP_BASE_URL / WP_USER / WP_APP_PASSWORD /
           X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET
  入力:   data/edinetcode.csv / data/notable_filers.txt
  状態:   data/posted.json （投稿・ツイート済みの日付。コミットバック）
"""
import os, sys, json
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taiyo_common as tc
import requests
import tweepy

NOTABLE_FILERS = "data/notable_filers.txt"
POSTED_JSON    = "data/posted.json"
TWEET_EXCLUDE  = "data/tweet_exclude.txt"   # 注目だが「ツイートはしない」提出者(記事・ウィジェットには残る)
CATEGORY       = "未分類"
HASHTAGS       = "#大量保有 #日本株"
TWEET_LIMIT    = 280   # Xの重み付き上限(全角=2,半角=1)
URL_WEIGHT     = 23    # XはURLを t.co 短縮で一律23として数える

WP_BASE = os.environ.get("WP_BASE_URL", "").rstrip("/")
WP_USER = os.environ.get("WP_USER", "")
WP_PASS = os.environ.get("WP_APP_PASSWORD", "")
X_KEYS = {k: os.environ.get(k, "") for k in
          ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET")}


def log(*a): print("[taiyo-daily]", *a, flush=True)


# ── 状態(投稿/ツイート済み日付) ──
def load_state() -> dict:
    if os.path.exists(POSTED_JSON):
        try:
            with open(POSTED_JSON, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"posted": {}, "tweeted": []}   # posted: {date: post_url}, tweeted: [date,...]


def save_state(st: dict):
    os.makedirs(os.path.dirname(POSTED_JSON), exist_ok=True)
    with open(POSTED_JSON, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=0)


def load_notable() -> list:
    if os.path.exists(NOTABLE_FILERS):
        with open(NOTABLE_FILERS, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return []


def load_exclude() -> list:
    """ツイート除外リスト(記事・ウィジェットには残すが、ツイートには出さない提出者)。"""
    if os.path.exists(TWEET_EXCLUDE):
        with open(TWEET_EXCLUDE, encoding="utf-8") as f:
            return [l.strip() for l in f if l.strip() and not l.startswith("#")]
    return []


def is_notable(r, notable):
    filer = (r.get("filer") or "").lower()
    return any(k.lower() in filer for k in notable)


def is_tweet_excluded(r, exclude):
    filer = (r.get("filer") or "").lower()
    return any(k.lower() in filer for k in exclude)


def esc(s):
    s = "" if s is None else str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def pct(x):
    return "—" if x is None else f"{x:.2f}%"


# ── 記事本文(HTML)を組み立て ──
def build_html(reports, notable, date_str):
    n_notable = sum(1 for r in reports if is_notable(r, notable))
    head = (f"<p>{esc(date_str)}に提出された大量保有報告書（新規取得）の一覧です。"
            f"全{len(reports)}件、うち著名投資家による提出が{n_notable}件。"
            f"保有割合は各報告書の原本（EDINET）に基づきます。</p>")
    rows = []
    rows.append("<table><thead><tr>"
                "<th>提出</th><th>銘柄</th><th>提出者（大量保有者）</th>"
                "<th>保有割合</th><th>保有目的</th><th>原本</th></tr></thead><tbody>")
    for r in sorted(reports, key=lambda x: x.get("submit",""), reverse=True):
        hot = is_notable(r, notable)
        style = ' style="background:#fbeaea;"' if hot else ""
        star = "★" if hot else ""
        nm = f'<strong style="color:#c0392b;">{star}{esc(r["name"])}</strong>' if hot else f'<strong>{esc(r["name"])}</strong>'
        doc = f'<a href="{esc(r["docUrl"])}" target="_blank" rel="noopener">原本</a>' if r.get("docUrl") else "—"
        t = (r.get("submit","") or "")[5:16].replace("-", "/")
        rows.append(
            f'<tr{style}><td>{esc(t)}</td>'
            f'<td>{esc(r["code"])} {nm}</td>'
            f'<td>{esc(r["filer"])}</td>'
            f'<td style="text-align:right;">{pct(r.get("ratio"))}</td>'
            f'<td>{esc(r.get("purpose") or "—")}</td>'
            f'<td>{doc}</td></tr>')
    rows.append("</tbody></table>")
    foot = ('<p style="font-size:12px;color:#666;">出典: EDINET（金融庁 電子開示システム）。'
            '提出時刻はJST。投資判断はご自身の責任で行ってください。</p>')
    return head + "\n".join(rows) + foot


def build_title(reports, notable, date_str):
    names = []
    for r in reports:
        if is_notable(r, notable):
            f = r["filer"]
            if f not in names:
                names.append(f)
    if names:
        clean = [n.replace("株式会社", "").strip() for n in names]
        tag = "／".join(clean[:2]) + ("他" if len(clean) > 2 else "")
        return f"{date_str} 大量保有報告書（著名投資家：{tag}）"
    return f"{date_str} 大量保有報告書（新規取得 {len(reports)}件）"


# ── WordPress投稿(タイムアウト耐性: 数回リトライ) ──
def wp_create_post(title, html):
    import time
    url = f"{WP_BASE}/wp-json/wp/v2/posts"
    payload = {"title": title, "content": html, "status": "publish"}
    last = None
    # ロリポップは海外IP/混雑で断続的にタイムアウトすることがあるので
    # 間隔をあけて最大5回トライする(15→30→45→60秒待ち)。
    for attempt in range(1, 6):
        try:
            r = requests.post(url, auth=(WP_USER, WP_PASS), json=payload, timeout=90)
            r.raise_for_status()
            return r.json().get("link")
        except requests.exceptions.RequestException as e:
            last = e
            wait = 15 * attempt
            log(f"WordPress接続 失敗({attempt}/5): {type(e).__name__}。{wait}秒後に再試行")
            if attempt < 5:
                time.sleep(wait)
    raise last



# ── Xの重み付き文字数(全角=2,半角=1, URLは23) ──
def x_weighted_len(text, url):
    t = text.replace(url, "x" * URL_WEIGHT)   # URLは23固定として数える
    w = 0
    for ch in t:
        cp = ord(ch)
        if (0x1100 <= cp <= 0x115F or 0x2E80 <= cp <= 0x303E or
            0x3041 <= cp <= 0x33FF or 0x3400 <= cp <= 0x4DBF or
            0x4E00 <= cp <= 0x9FFF or 0xA000 <= cp <= 0xA4CF or
            0xAC00 <= cp <= 0xD7A3 or 0xF900 <= cp <= 0xFAFF or
            0xFE30 <= cp <= 0xFE4F or 0xFF00 <= cp <= 0xFF60 or
            0xFFE0 <= cp <= 0xFFE6 or cp >= 0x1F000):
            w += 2
        else:
            w += 1
    return w


# ── ツイート組み立て(収まるだけ詰める) ──
def build_tweet(tweet_hots, url, date_str):
    hots = sorted(tweet_hots, key=lambda x: (x.get("ratio") or 0), reverse=True)  # 保有割合が高い順
    # ヘッダーに実行時刻(分)を入れて毎回ユニークにする(重複403回避)。
    hhmm = tc.now_jst().strftime("%H:%M")
    header = f"【著名投資家の大量保有】{date_str[5:]} {hhmm}時点\n"
    tail = f"\n詳細▼\n{url}\n{HASHTAGS}"
    # 行を1件ずつ足し、ヘッダー+行+末尾の合計がXの上限(280)に収まる範囲だけ採用
    lines = []
    for r in hots:
        line = f'★{r["filer"][:12]}→{r["name"][:10]}({r["code"]}) {pct(r.get("ratio"))}\n'
        candidate = header + "".join(lines) + line + tail
        if x_weighted_len(candidate, url) <= TWEET_LIMIT:
            lines.append(line)
        else:
            break
    if not lines:   # 1件も入らない極端な場合は最低1件を短縮で
        r = hots[0]
        lines = [f'★{r["name"][:8]}({r["code"]}) {pct(r.get("ratio"))}\n']
    return header + "".join(lines) + tail


def x_client():
    return tweepy.Client(
        consumer_key=X_KEYS["X_API_KEY"], consumer_secret=X_KEYS["X_API_SECRET"],
        access_token=X_KEYS["X_ACCESS_TOKEN"], access_token_secret=X_KEYS["X_ACCESS_SECRET"])


def main():
    need = {"EDINET_API_KEY": os.environ.get("EDINET_API_KEY",""),
            "WP_BASE_URL": WP_BASE, "WP_USER": WP_USER, "WP_APP_PASSWORD": WP_PASS}
    miss = [k for k,v in need.items() if not v]
    if miss:
        sys.exit("環境変数が不足: " + ", ".join(miss))

    st = load_state()
    notable = load_notable()
    ecmap = tc.load_edinet_map()

    # ── 対象日の決定 ──
    # 実行時刻の日付固定だと、夜間実行/遅延/タイムゾーンで簡単に1日ズレる。
    # そこで「当日から遡り、初回大量保有(350)が実際に1件以上ある最初の日」を対象にする。
    # これで何時に走っても、直近で提出があった営業日を自己修正で拾える。
    now = tc.now_jst()
    today = None
    reports = []
    fallback = None   # 提出はあるが既に投稿済みの直近日(全部投稿済みのときの保険)
    for back in range(0, 6):                     # 最大6日さかのぼる(連休対策)
        d = (now - dt.timedelta(days=back))
        if d.weekday() >= 5:                     # 土日はそもそも提出なし→スキップ
            continue
        ds = d.strftime("%Y-%m-%d")
        docs = tc.fetch_docs(date=ds)
        reps = [r for r in tc.extract_reports(docs, ecmap) if r["isNew"]]
        log(f"{ds}: 新規取得 {len(reps)} 件")
        if not reps:
            continue
        if ds in st["posted"]:                   # 既に記事化済みの日はスキップ(取り戻し不要)
            if fallback is None:
                fallback = (ds, reps, d)
            continue
        today, reports, target = ds, reps, d     # 未投稿で提出がある最初の日を採用
        break

    if today is None and fallback is not None:
        # 直近の提出日はすべて投稿済み → その最新日を対象に(ツイート未送なら今日送れる)
        today, reports, target = fallback

    if not reports:
        log("直近に初回大量保有の提出が見つからず。スキップ")
        return
    date_jp = target.strftime("%-m月%-d日") if os.name != "nt" else target.strftime("%m月%d日")
    log(f"対象日: {today}（新規取得 {len(reports)} 件）")

    # 保有割合を付与
    tc.enrich_with_ratio(reports, sleep=0.2)

    # ── 記事(1日1本・重複防止) ──
    post_url = st["posted"].get(today)
    if post_url:
        log(f"本日の記事は投稿済み: {post_url}（再投稿しない）")
    else:
        title = build_title(reports, notable, date_jp)
        html  = build_html(reports, notable, date_jp)
        post_url = wp_create_post(title, html)
        st["posted"][today] = post_url
        save_state(st)
        log(f"記事投稿: {post_url}")

    # ── ツイート(注目投資家がいる日だけ・1回・重複防止) ──
    if today in st["tweeted"]:
        log("本日はツイート済み。スキップ")
        return
    # 注目投資家のうち、ツイート除外リスト(Evo Fund等)を除いたものだけツイート対象
    exclude = load_exclude()
    hots = [r for r in reports if is_notable(r, notable)]
    tweet_hots = [r for r in hots if not is_tweet_excluded(r, exclude)]
    n_excluded = len(hots) - len(tweet_hots)
    if n_excluded:
        log(f"ツイート除外: {n_excluded} 件(記事には掲載・ツイートのみ除外)")
    if not tweet_hots:
        log("ツイート対象の注目投資家なし(除外後)。ツイートはスキップ")
        return
    text = build_tweet(tweet_hots, post_url, date_jp)
    log(f"ツイート本文（{len(text)}字）:\n{text}")
    try:
        resp = x_client().create_tweet(text=text)
        st["tweeted"].append(today)
        save_state(st)
        log(f"ツイート完了（対象 {len(tweet_hots)} 件中、収まるぶん） id={resp.data.get('id')}")
    except tweepy.Forbidden as e:
        # 403の詳細(重複 duplicate か、権限 permission かを切り分ける)
        detail = getattr(e, "api_messages", None) or getattr(e, "api_errors", None) or str(e)
        log(f"ツイート失敗 403 Forbidden 詳細: {detail}")
    except Exception as e:
        log(f"ツイート失敗（次回再試行）: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
