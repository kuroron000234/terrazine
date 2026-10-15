"""RSS などを集めて site/ に TERRAZINE の静的ページを生成する。

使い方:
    python build.py                 # 収集して site/index.html, site/items.json を出力
    PREV_URL=https://.../items.json python build.py   # 前回分とマージ (Actions で使用)
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.utils import format_datetime
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import feedparser
import requests

ROOT = Path(__file__).parent
OUT = ROOT / "site"
KEEP_DAYS = 7
DEFAULT_LIMIT = 50
MAX_OG_FETCH = 200  # 1回の実行で og:image を取りに行く最大記事数
UA = "Mozilla/5.0 (compatible; terrazine/1.0; personal feed reader)"
SITE_URL = os.environ.get("SITE_URL", "https://kuroron000234.github.io/terrazine").rstrip("/")
FEED_ITEMS = 200
REGION_CODE = {"ja": "JP", "en": "EN", "zh": "CN"}
# RSS の出力先: ファイル名 → (タイトル, 記事の条件)
FEEDS_OUT = {
    "all": ("TERRAZINE", lambda it: True),
    "ja": ("TERRAZINE / 日本", lambda it: it["region"] == "ja"),
    "en": ("TERRAZINE / 英語圏", lambda it: it["region"] == "en"),
    "zh": ("TERRAZINE / 中華圏", lambda it: it["region"] == "zh"),
    "ai": ("TERRAZINE / #AI", lambda it: "ai" in it["tags"]),
    "dev": ("TERRAZINE / #DEV", lambda it: "dev" in it["tags"]),
    "hw": ("TERRAZINE / #HW", lambda it: "hw" in it["tags"]),
}

# 英数字キーワードは前後が英数字でない位置でだけ一致させる (AI が "said" に当たらないように)。
# 先頭に "=" を付けたものは大文字小文字を区別する。
KEYWORDS: dict[str, list[str]] = {
    "ai": [
        "=AI", "AGI", "LLM", "LLMs", "GPT", "ChatGPT", "OpenAI", "Anthropic", "Claude", "Gemini",
        "DeepSeek", "Qwen", "Kimi", "Moonshot", "MiniMax", "GLM", "Zhipu", "Doubao", "ERNIE",
        "Hunyuan", "StepFun", "Llama", "Mistral", "Grok", "xAI", "Copilot", "chatbot", "agent",
        "agents", "agentic", "machine learning", "deep learning", "neural", "transformer",
        "diffusion", "multimodal", "inference", "reasoning model", "humanoid", "embodied",
        "AIGC", "Hugging Face",
        # 中国語
        "人工智能", "大模型", "模型", "智能体", "机器学习", "深度学习", "神经网络", "生成式",
        "多模态", "推理模型", "具身智能", "人形机器人", "机器人", "通义", "千问", "月之暗面", "智谱",
        "豆包", "文心", "混元", "阶跃星辰", "百川", "零一万物", "元宝", "算法",
        # 日本語
        "人工知能", "生成AI", "言語モデル", "機械学習", "ディープラーニング", "エージェント",
        "チャットボット", "推論モデル", "ロボット", "ヒューマノイド",
    ],
    "hw": [
        "chip", "chips", "chipmaker", "semiconductor", "semiconductors", "GPU", "GPUs", "CPU",
        "CPUs", "NPU", "TPU", "Nvidia", "AMD", "Intel", "TSMC", "ASML", "SMIC", "Huawei",
        "Ascend", "Kirin", "HarmonyOS", "Qualcomm", "Snapdragon", "=Arm", "RISC-V", "HBM",
        "DRAM", "NAND", "SSD", "wafer", "foundry", "lithography", "datacenter", "data center",
        "data centers", "supercomputer", "quantum", "Cambricon", "CXMT", "YMTC", "Apple Silicon",
        "processor", "processors", "server", "servers", "Rapidus", "iPhone", "Pixel", "Android",
        # 中国語
        "芯片", "半导体", "晶圆", "光刻", "英伟达", "英特尔", "台积电", "中芯", "华为", "昇腾",
        "麒麟", "鸿蒙", "高通", "骁龙", "处理器", "显卡", "内存", "存储", "数据中心", "算力",
        "服务器", "寒武纪", "摩尔线程", "壁仞", "海光", "长鑫", "长江存储", "玄戒", "制程",
        "量子计算", "超算",
        # 日本語
        "半導体", "チップ", "エヌビディア", "インテル", "ラピダス", "ファーウェイ", "プロセッサ",
        "グラボ", "グラフィックボード", "ビデオカード", "メモリ", "データセンター", "スパコン",
        "量子コンピュータ", "スマホ", "スマートフォン", "自作PC",
    ],
    "dev": [
        "GitHub", "open source", "open-source", "programming", "programmer", "programmers",
        "developer", "developers", "Rust", "Python", "JavaScript", "TypeScript", "Kubernetes",
        "Linux", "=API", "APIs", "SDK", "framework", "compiler", "database", "IDE", "VS Code",
        "Cursor", "coding", "WebAssembly", "Wasm", "CVE", "zero-day", "0-day", "OSS",
        # 中国語
        "开源", "程序员", "编程", "开发者", "代码", "编译器", "数据库", "框架", "漏洞",
        # 日本語
        "【IT】", "プログラミング", "プログラマ", "エンジニア", "開発者", "オープンソース",
        "脆弱性", "ゼロデイ", "ライブラリ", "フレームワーク", "データベース", "サイバー攻撃",
    ],
}


def _compile_keywords() -> dict[str, re.Pattern]:
    out = {}
    for tag, words in KEYWORDS.items():
        cs, ci = [], []
        for w in words:
            sensitive = w.startswith("=")
            w = w.lstrip("=")
            pat = re.escape(w)
            if re.match(r"[A-Za-z0-9]", w):
                pat = rf"(?<![A-Za-z0-9]){pat}(?![A-Za-z0-9])"
            (cs if sensitive else ci).append(pat)
        parts = []
        if ci:
            parts.append("(?i:" + "|".join(ci) + ")")
        if cs:
            parts.append("|".join(cs))
        out[tag] = re.compile("|".join(parts))
    return out


KEYWORD_RE = _compile_keywords()

TRACKING_PARAMS = re.compile(r"^(utm_|spm|from$|source$|ref$|fbclid|gclid)")
# 記事のサムネイルとして使わない画像 (はてブ数バッジ、トラッキングピクセル、アバターなど)
BAD_IMAGE = re.compile(r"b\.hatena\.ne\.jp/entry/image|st-hatena|feeds\.feedburner|/pixel|gravatar|avatars\.githubusercontent|emoji|\.gif(\?|$)", re.I)


def normalize_url(url: str) -> str:
    s = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(s.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((s.scheme or "https", s.netloc.lower(), s.path, urlencode(q), ""))


def clean_text(raw: str, limit: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    raw = entry.get("published") or entry.get("updated") or ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?", raw)
    if m:
        tz = m.group(3) or "+0800"
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)} {tz}", "%Y-%m-%d %H:%M:%S %z").astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def match_tags(text: str) -> list[str]:
    return [tag for tag, rx in KEYWORD_RE.items() if rx.search(text)]


def feed_image(e) -> str | None:
    """フィード内に含まれる画像URLを探す。"""
    candidates = [e.get("hatena_imageurl")]
    candidates += [m.get("url") for m in e.get("media_thumbnail", [])]
    candidates += [m.get("url") for m in e.get("media_content", []) if m.get("medium", "image") == "image"]
    candidates += [l.get("href") for l in e.get("links", []) if l.get("rel") == "enclosure" and l.get("type", "").startswith("image")]
    body = (e.get("summary") or "") + "".join(c.get("value", "") for c in e.get("content", []))
    candidates += re.findall(r"<img[^>]+src=[\"']([^\"']+)", body)
    for c in candidates:
        if c and c.startswith("http") and not BAD_IMAGE.search(c):
            return html.unescape(c)
    return None


def make_item(feed: dict, *, title: str, link: str, summary: str, published: datetime | None, **extra) -> dict | None:
    if feed.get("exclude") and re.search(feed["exclude"], title):
        return None
    tags = set(feed.get("tags", []))
    matched = match_tags(f"{title} {summary}")
    if feed.get("filter") and not matched:
        return None
    tags.update(matched)
    now = datetime.now(timezone.utc)
    if published and published > now:
        published = now
    url = normalize_url(link)
    item = {
        "id": hashlib.sha1(url.encode()).hexdigest()[:16],
        "title": title,
        "link": link,
        "summary": summary if summary != title else "",
        "source": feed["name"],
        "region": feed["region"],
        "tags": sorted(tags),
        "published": (published or now).isoformat(timespec="seconds"),
    }
    if not published:
        item["_nodate"] = True  # merge で前回の時刻を引き継ぐ
    item.update({k: v for k, v in extra.items() if v})
    if feed.get("og") is False and "image" not in item:
        item["image"] = ""  # og:image を取りに行かない
    return item


def fetch_rss(feed: dict) -> list[dict]:
    r = requests.get(feed["url"], timeout=25, headers={"User-Agent": UA})
    r.raise_for_status()
    parsed = feedparser.parse(r.content)
    items = []
    for e in parsed.entries[: feed.get("limit", DEFAULT_LIMIT)]:
        link, title = e.get("link"), clean_text(e.get("title", ""), 300)
        if not link or not title:
            continue
        raw = e.get("summary") or e.get("description") or ""
        score = None
        if m := re.search(r"Points: (\d+)", raw):  # hnrss
            score = int(m.group(1))
        summary = clean_text(raw)
        if summary.startswith("Article URL:"):  # hnrss の本文は URL とポイントだけ
            summary = ""
        summary = re.sub(r"\s*submitted by /u/\S+.*$", "", summary)  # Reddit の定型文
        comments = e.get("comments")
        via = None
        if "reddit.com" in feed["url"] and e.get("tags"):
            via = "r/" + e["tags"][0]["term"]
        item = make_item(
            feed, title=title, link=link, summary=summary, published=parse_date(e),
            image=feed_image(e), score=score, via=via,
            comments=comments if comments and comments != link else None,
        )
        if item:
            items.append(item)
    return items


def fetch_5ch(feed: dict) -> list[dict]:
    """5ch の板の subject.txt から、勢いのある新しめのスレを拾う。"""
    board = feed["url"].rstrip("/")
    host, name = board.rsplit("/", 1)
    r = requests.get(f"{board}/subject.txt", timeout=25, headers={"User-Agent": UA})
    r.raise_for_status()
    now = time.time()
    threads = []
    for line in r.content.decode("cp932", errors="replace").splitlines():
        m = re.match(r"(\d+)\.dat<>(.*) \((\d+)\)$", line)
        if not m:
            continue
        key, res = int(m.group(1)), int(m.group(3))
        hours = (now - key) / 3600
        if not (0 < hours < 48) or res < feed.get("min_res", 50):
            continue
        title = html.unescape(m.group(2))
        title = re.sub(r"\s*\[[^\]]*★\]\s*$", "", title)  # 記者名 [ばーど★] などを外す
        threads.append((res / max(hours, 0.5), key, res, title))
    threads.sort(reverse=True)
    items = []
    for momentum, key, res, title in threads[: feed.get("limit", 15)]:
        item = make_item(
            feed, title=title, link=f"{host}/test/read.cgi/{name}/{key}/", summary="",
            published=datetime.fromtimestamp(key, timezone.utc), replies=res,
        )
        if item:
            items.append(item)
    return items


def fetch_feed(feed: dict) -> list[dict]:
    try:
        items = (fetch_5ch if feed.get("type") == "5ch" else fetch_rss)(feed)
    except Exception as e:  # 1つのフィードが落ちても全体は続ける
        print(f"  ! {feed['name']}: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    print(f"  {feed['name']}: {len(items)}件")
    return items


def fetch_og_image(item: dict) -> None:
    """記事ページの <head> から og:image / twitter:image を取る。見つからなければ空文字。"""
    item["image"] = ""
    try:
        with requests.get(item["link"], timeout=8, headers={"User-Agent": UA}, stream=True) as r:
            if not r.ok or "html" not in r.headers.get("content-type", ""):
                return
            head = b""
            for chunk in r.iter_content(16384):
                head += chunk
                if b"</head>" in head or len(head) > 400_000:
                    break
        text = head.decode(r.encoding or "utf-8", errors="replace")
        for prop in ("og:image", "twitter:image", "og:image:url"):
            m = (re.search(rf'<meta[^>]+(?:property|name)=["\']{prop}["\'][^>]+content=["\']([^"\']+)', text, re.I)
                 or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{prop}["\']', text, re.I))
            if m:
                url = urljoin(item["link"], html.unescape(m.group(1).strip()))
                if url.startswith("http") and not BAD_IMAGE.search(url):
                    item["image"] = url
                return
    except Exception:
        pass


def load_previous() -> list[dict]:
    url = os.environ.get("PREV_URL")
    path = OUT / "items.json"
    try:
        if url:
            r = requests.get(url, timeout=20)
            if r.ok:
                return r.json()["items"]
        elif path.exists():
            return json.loads(path.read_text("utf-8"))["items"]
    except Exception as e:
        print(f"  ! 前回データを読めませんでした: {e}", file=sys.stderr)
    return []


def merge(prev: list[dict], fresh: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {it["id"]: it for it in prev}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for it in fresh:
        old = by_id.get(it["id"])
        nodate = it.pop("_nodate", False)
        if old:
            # 翻訳・画像など後から付けたフィールドと初出時刻は残す
            it = {**old, **it, "first_seen": old.get("first_seen", it["published"])}
            if nodate:  # 日付のないフィードは毎回「今」にならないよう初出時刻のまま
                it["published"] = old["published"]
        else:
            it["first_seen"] = now
        by_id[it["id"]] = it
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    items = [it for it in by_id.values() if datetime.fromisoformat(it["published"]) >= cutoff]
    items.sort(key=lambda it: it["published"], reverse=True)
    return items


def rss_item(it: dict) -> str:
    """Feedly などで読みやすいよう、見出しに地域、本文にサムネイルとリンクを入れる。"""
    code = REGION_CODE[it["region"]]
    title = it.get("title_ja") or it["title"]
    body = []
    if it.get("image"):
        body.append(f'<p><img src="{escape(it["image"], {chr(34): "&quot;"})}" referrerpolicy="no-referrer"></p>')
    if it.get("title_ja"):
        body.append(f"<p>{escape(it['title'])}</p>")
    if it.get("summary_ja") or it.get("summary"):
        body.append(f"<p>{escape(it.get('summary_ja') or it['summary'])}</p>")
    meta = [f"[{code}] {escape(it['source'])}" + (f" · {escape(it['via'])}" if it.get("via") else "")]
    if it.get("score"):
        meta.append(f"▲{it['score']}")
    if it.get("replies"):
        meta.append(f"{it['replies']}レス")
    meta += [f"#{t.upper()}" for t in it["tags"]]
    links = []
    if it["region"] != "ja":
        tr = "https://translate.google.com/translate?sl=auto&tl=ja&hl=ja&u=" + quote(it["link"], safe="")
        links.append(f'<a href="{escape(tr)}">日本語で読む</a>')
    if it.get("comments"):
        links.append(f'<a href="{escape(it["comments"])}">コメント</a>')
    body.append("<p>" + " ".join(meta) + ("<br>" + " | ".join(links) if links else "") + "</p>")
    image = ""
    if it.get("image"):
        image = f'<media:content url="{escape(it["image"], {chr(34): "&quot;"})}" medium="image"/>'
    cats = "".join(f"<category>{t}</category>" for t in [code, *it["tags"]])
    return (
        f"<item><title>{escape(f'[{code}] {title}')}</title><link>{escape(it['link'])}</link>"
        f'<guid isPermaLink="false">{it["id"]}</guid>'
        f"<pubDate>{format_datetime(datetime.fromisoformat(it['published']))}</pubDate>"
        f"<dc:creator>{escape(it['source'])}</dc:creator>{cats}{image}"
        f"<description>{escape(''.join(body))}</description></item>"
    )


def write_rss(items: list[dict], generated: datetime) -> None:
    for name, (title, cond) in FEEDS_OUT.items():
        selected = [it for it in items if cond(it)][:FEED_ITEMS]
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            f"<title>{escape(title)}</title><link>{SITE_URL}/</link>"
            f'<atom:link href="{SITE_URL}/{name}.xml" rel="self" type="application/rss+xml"/>'
            "<description>日本・英語圏・中華圏のテックニュース</description><language>ja</language>"
            f"<lastBuildDate>{format_datetime(generated)}</lastBuildDate>"
            + "".join(rss_item(it) for it in selected)
            + "</channel></rss>\n"
        )
        (OUT / f"{name}.xml").write_text(xml, "utf-8")


def main() -> None:
    feeds = tomllib.loads((ROOT / "feeds.toml").read_text("utf-8"))["feed"]
    print(f"{len(feeds)}フィードを取得中…")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        fresh = [it for batch in ex.map(fetch_feed, feeds) for it in batch]
    items = merge(load_previous(), fresh)

    need_og = [it for it in items if "image" not in it][:MAX_OG_FETCH]
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(fetch_og_image, need_og))
    with_img = sum(1 for it in items if it.get("image"))
    print(f"新規取得 {len(fresh)}件 / 保持 {len(items)}件 / 画像あり {with_img}件 "
          f"(og:image {len(need_og)}件取得, {time.time() - t0:.1f}秒)")

    generated = datetime.now(timezone.utc)
    data = {
        "generated": generated.isoformat(timespec="seconds"),
        "sources": [{"name": f["name"], "region": f["region"]} for f in feeds],
        "items": items,
    }
    OUT.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (OUT / "items.json").write_text(payload, "utf-8")
    template = (ROOT / "web" / "index.html").read_text("utf-8")
    page = template.replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    # Apps Script (HtmlService) 経由で表示すると、スクリプト中の "//" 以降がコメント扱いで
    # 削られて壊れるため、コメント行を消してから文字列中の "//" を "/\/" (同じ意味) に置き換える
    head, script = page.split("<script>", 1)
    script, tail = script.split("</script>", 1)
    script = re.sub(r"(?m)^\s*//.*\n", "", script).replace("//", "/\\/")
    page = f"{head}<script>{script}</script>{tail}"
    (OUT / "index.html").write_text(page, "utf-8")
    write_rss(items, generated)
    print(f"→ {OUT / 'index.html'}")


if __name__ == "__main__":
    main()
