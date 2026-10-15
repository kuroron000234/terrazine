"""RSS を集めて site/ に静的サイトを生成する。

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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

ROOT = Path(__file__).parent
OUT = ROOT / "site"
KEEP_DAYS = 7
DEFAULT_LIMIT = 50
UA = "Mozilla/5.0 (compatible; technews-bot/1.0; personal feed reader)"

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
        "人工智能", "大模型", "模型", "智能体", "机器学习", "深度学习", "神经网络", "生成式",
        "多模态", "推理模型", "具身智能", "人形机器人", "机器人", "通义", "千问", "月之暗面", "智谱",
        "豆包", "文心", "混元", "阶跃星辰", "百川", "零一万物", "元宝", "算法",
    ],
    "hw": [
        "chip", "chips", "chipmaker", "semiconductor", "semiconductors", "GPU", "GPUs", "CPU",
        "CPUs", "NPU", "TPU", "Nvidia", "AMD", "Intel", "TSMC", "ASML", "SMIC", "Huawei",
        "Ascend", "Kirin", "HarmonyOS", "Qualcomm", "Snapdragon", "=Arm", "RISC-V", "HBM",
        "DRAM", "NAND", "wafer", "foundry", "lithography", "datacenter", "data center",
        "data centers", "supercomputer", "quantum", "Cambricon", "CXMT", "YMTC", "Apple Silicon",
        "processor", "processors", "server", "servers",
        "芯片", "半导体", "晶圆", "光刻", "英伟达", "英特尔", "台积电", "中芯", "华为", "昇腾",
        "麒麟", "鸿蒙", "高通", "骁龙", "处理器", "显卡", "内存", "存储", "数据中心", "算力",
        "服务器", "寒武纪", "摩尔线程", "壁仞", "海光", "长鑫", "长江存储", "玄戒", "制程",
        "量子计算", "超算",
    ],
    "dev": [
        "GitHub", "open source", "open-source", "programming", "programmer", "programmers",
        "developer", "developers", "Rust", "Python", "JavaScript", "TypeScript", "Kubernetes",
        "Linux", "=API", "APIs", "SDK", "framework", "compiler", "database", "IDE", "VS Code",
        "Cursor", "coding", "WebAssembly", "Wasm", "CVE", "zero-day", "0-day",
        "开源", "程序员", "编程", "开发者", "代码", "编译器", "数据库", "框架", "漏洞",
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
    # feedparser が読めない形式 (例: 36氪 "2026-10-15 08:13:46  +0800")
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


def fetch_feed(feed: dict) -> list[dict]:
    try:
        r = requests.get(feed["url"], timeout=25, headers={"User-Agent": UA})
        r.raise_for_status()
    except Exception as e:  # 1つのフィードが落ちても全体は続ける
        print(f"  ! {feed['name']}: {type(e).__name__}: {e}", file=sys.stderr)
        return []
    parsed = feedparser.parse(r.content)
    now = datetime.now(timezone.utc)
    items = []
    for e in parsed.entries[: feed.get("limit", DEFAULT_LIMIT)]:
        link = e.get("link")
        title = clean_text(e.get("title", ""), 300)
        if not link or not title:
            continue
        summary = clean_text(e.get("summary") or e.get("description") or "")
        tags = set(feed.get("tags", []))
        matched = match_tags(f"{title} {summary}")
        if feed.get("filter") and not matched:
            continue
        tags.update(matched)
        published = parse_date(e)
        if published and published > now:
            published = now
        url = normalize_url(link)
        comments = e.get("comments")
        items.append({
            "id": hashlib.sha1(url.encode()).hexdigest()[:16],
            "title": title,
            "link": link,
            "summary": summary if summary != title else "",
            "source": feed["name"],
            "region": feed["region"],
            "tags": sorted(tags),
            "published": (published or now).isoformat(timespec="seconds"),
            "comments": comments if comments and comments != link else None,
        })
    print(f"  {feed['name']}: {len(items)}件")
    return items


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
    by_id: dict[str, dict] = {}
    for it in prev:
        by_id[it["id"]] = it
    for it in fresh:
        old = by_id.get(it["id"])
        if old:
            # 翻訳など後から付けたフィールドと初出時刻は残す
            it = {**old, **it, "first_seen": old.get("first_seen", it["published"])}
        else:
            it["first_seen"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        by_id[it["id"]] = it
    cutoff = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
    items = [it for it in by_id.values() if datetime.fromisoformat(it["published"]) >= cutoff]
    items.sort(key=lambda it: it["published"], reverse=True)
    return items


def main() -> None:
    feeds = tomllib.loads((ROOT / "feeds.toml").read_text("utf-8"))["feed"]
    print(f"{len(feeds)}フィードを取得中…")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=10) as ex:
        fresh = [it for batch in ex.map(fetch_feed, feeds) for it in batch]
    items = merge(load_previous(), fresh)
    print(f"新規取得 {len(fresh)}件 / 保持 {len(items)}件 ({time.time() - t0:.1f}秒)")

    data = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [{"name": f["name"], "region": f["region"]} for f in feeds],
        "items": items,
    }
    OUT.mkdir(exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    (OUT / "items.json").write_text(payload, "utf-8")
    template = (ROOT / "web" / "index.html").read_text("utf-8")
    page = template.replace("/*__DATA__*/null", payload.replace("</", "<\\/"))
    (OUT / "index.html").write_text(page, "utf-8")
    print(f"→ {OUT / 'index.html'}")


if __name__ == "__main__":
    main()
