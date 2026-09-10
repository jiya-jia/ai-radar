#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 雷达 · 数据抓取器
====================
- 并行抓取多个高质量 AI 媒体的 RSS 源（纯标准库，无第三方依赖）
- 按相关性与时效性评分，按北京时间归入对应日期
- 生成结构化 JSON 数据：data/news/YYYY-MM-DD.json + data/manifest.json
- 与已有数据合并去重，历史归档持续累积

用法:
    python3 scripts/fetch_news.py             # 常规更新（回看 2 天）
    python3 scripts/fetch_news.py --days 5    # 首次播种（建立更多历史归档）
"""

import argparse
import concurrent.futures
import email.utils
import html as html_lib
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
NEWS_DIR = os.path.join(DATA_DIR, "news")

TZ = timezone(timedelta(hours=8))   # 北京时间（本地与 CI 环境统一）

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FETCH_TIMEOUT = 20        # 单源抓取超时（秒）
MAX_PER_DAY = 16          # 单日收录条数上限
MAX_PER_SOURCE = 5        # 单一来源单日最多收录条数
SUMMARY_LIMIT = 160        # 摘要截断长度（字符）
MIN_ITEMS_TO_WRITE = 3    # 单日少于该条数不生成文件（避免稀疏归档）

# (名称, RSS地址)
FEEDS = [
    ("量子位", "https://www.qbitai.com/feed"),
    ("雷峰网", "https://www.leiphone.com/feed"),
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
    ("Ars Technica", "https://arstechnica.com/ai/feed/"),
    ("AI News", "https://artificialintelligence-news.com/feed/"),
    ("VentureBeat", "https://venturebeat.com/category/ai/feed/"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed/"),
]

# 关键词评分表: (权重, [关键词...])，ASCII 词按整词匹配，中文按包含匹配
KEYWORDS = [
    (3, ["openai", "anthropic", "gpt", "claude", "gemini", "deepseek", "llm",
         "大模型", "大语言模型", "人工智能", "智能体", "agent", "agi"]),
    (2, ["英伟达", "nvidia", "gpu", "芯片", "算力", "融资", "机器人", "humanoid",
         "robot", "自动驾驶", "多模态", "微软", "谷歌", "meta", "苹果", "特斯拉",
         "xai", "grok", "mistral", "智谱", "月之暗面", "kimi", "豆包", "文心",
         "通义", "minimax", "open source", "开源"]),
    (1, ["ai", "模型", "算法", "机器学习", "深度学习", "神经网络", "生成式",
         "aigc", "chatgpt", "copilot"]),
]

# 标题包含以下词的条目直接排除（非资讯类）
TITLE_BLOCKLIST = ["招聘", "荐岗", "直播预告", "活动报名", "福利", "赠书",
                   "订阅", "会员日", "周刊汇总", "日报汇总"]

log = logging.getLogger("ai-radar")

# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_html(text):
    """去除 HTML 标签与实体，压缩空白"""
    if not text:
        return ""
    text = html_lib.unescape(text)
    text = TAG_RE.sub(" ", text)
    text = html_lib.unescape(text)
    return WS_RE.sub(" ", text).strip()


def truncate(text, limit=SUMMARY_LIMIT):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", "，", ". ", " "):
        i = cut.rfind(sep)
        if i > limit * 0.55:
            return cut[:i].rstrip("，,;； ") + "…"
    return cut.rstrip("，,;； ") + "…"


def parse_date(s):
    """解析 RSS pubDate(RFC822) 或 Atom 时间(ISO8601)，返回带时区 datetime"""
    if not s:
        return None
    s = s.strip()
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            return dt.astimezone(TZ)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def norm_title(t):
    """标题归一化（用于去重）"""
    return re.sub(r"[\W_]+", "", (t or "").lower())


def keyword_score(text):
    score = 0
    for weight, words in KEYWORDS:
        for w in words:
            if re.search(r"[a-z0-9]", w):
                pattern = r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])"
                if re.search(pattern, text, re.I):
                    score += weight
            elif w in text.lower():
                score += weight
    return score


def detect_tag(title, desc):
    """分类检测"""
    text = (title + " " + desc).lower()
    for tag, words in [
        ("融资", ["融资", "亿美元", "raises", "funding", "估值", "valuation", "ipo", "上市"]),
        ("芯片", ["芯片", "gpu", "英伟达", "nvidia", "tpu", "半导体", "chip", "算力"]),
        ("机器人", ["机器人", "humanoid", "robot", "人形"]),
        ("政策", ["监管", "法案", "政策", "regulation", "lawsuit", "法院", "诉讼", "ban"]),
        ("研究", ["论文", "research", "study", "研究", "benchmark"]),
        ("模型", ["gpt", "claude", "gemini", "deepseek", "llm", "大模型", "模型",
                 "model", "多模态"]),
        ("产品", ["发布", "上线", "launch", "release", "推出", "开源", "open source"]),
    ]:
        if any(w in text for w in words):
            return tag
    return "资讯"


# ----------------------------------------------------------------------------
# 抓取与解析
# ----------------------------------------------------------------------------
def fetch_url(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, "
                  "text/xml, */*",
    })
    last_err = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            log.warning("抓取失败(%d/2) %s: %s", attempt + 1, url, e)
    raise last_err


ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def parse_feed(xml_bytes, source_name):
    """解析 RSS 2.0 / Atom，返回条目列表"""
    root = ET.fromstring(xml_bytes)
    items = []

    def make_item(title, link, desc, dt):
        return {"source": source_name, "title": clean_html(title) if title else "",
                "link": (link or "").strip(),
                "desc": clean_html(desc) if desc else "",
                "dt": dt}

    if root.tag == "rss" or root.tag.endswith("rss}"):
        channel = root.find("channel")
        if channel is None:
            return items
        for it in channel.findall("item"):
            title = it.findtext("title") or ""
            link = it.findtext("link") or ""
            desc = it.findtext("description") or ""
            rich = it.findtext(CONTENT_NS + "encoded") or ""
            dt = parse_date(it.findtext("pubDate"))
            if len(clean_html(rich)) > len(clean_html(desc)) + 20:
                desc = rich
            items.append(make_item(title, link, desc, dt))
    elif root.tag == ATOM_NS + "feed":
        for e in root.findall(ATOM_NS + "entry"):
            title = e.findtext(ATOM_NS + "title") or ""
            link = ""
            for l in e.findall(ATOM_NS + "link"):
                if l.get("rel") in (None, "alternate"):
                    link = l.get("href") or ""
                    break
            desc = e.findtext(ATOM_NS + "summary") or ""
            rich = e.findtext(ATOM_NS + "content") or ""
            if len(clean_html(rich)) > len(clean_html(desc)) + 20:
                desc = rich
            dt = parse_date(e.findtext(ATOM_NS + "published")
                            or e.findtext(ATOM_NS + "updated"))
            items.append(make_item(title, link, desc, dt))
    return items


def fetch_feed(source_name, url):
    try:
        xml_bytes = fetch_url(url)
        items = parse_feed(xml_bytes, source_name)
        log.info("✓ %-16s %d 条", source_name, len(items))
        return items
    except Exception as e:
        log.warning("✗ %s 抓取/解析失败: %s", source_name, e)
        return []


# ----------------------------------------------------------------------------
# 筛选、按日分桶
# ----------------------------------------------------------------------------
def select_by_day(all_items, now, days):
    """评分筛选 → 按北京时间归入日期桶"""
    cutoff = now - timedelta(hours=days * 24 + 6)
    future_limit = now + timedelta(hours=6)

    scored = []
    for it in all_items:
        if not it["title"] or not it["link"] or not it["dt"]:
            continue
        if not (cutoff <= it["dt"] <= future_limit):
            continue
        if any(b in it["title"] for b in TITLE_BLOCKLIST):
            continue
        score = keyword_score(it["title"]) * 2 + min(keyword_score(it["desc"]), 6)
        if score <= 0:
            continue
        it["score"] = score
        scored.append(it)

    buckets = {}
    for it in scored:
        buckets.setdefault(it["dt"].strftime("%Y-%m-%d"), []).append(it)

    result = {}
    for date_str, lst in buckets.items():
        # 日期桶内：来源内按分数去重 → 单来源限量 → 多来源轮转选优
        by_source = {}
        for it in lst:
            by_source.setdefault(it["source"], []).append(it)
        for src in by_source:
            deduped, seen_src = [], set()
            for it in sorted(by_source[src],
                             key=lambda x: (-x["score"], -x["dt"].timestamp())):
                key = norm_title(it["title"])[:18]
                if key in seen_src:
                    continue
                seen_src.add(key)
                deduped.append(it)
            by_source[src] = deduped[:MAX_PER_SOURCE]

        selected, seen = [], set()
        pools = [list(v) for v in by_source.values()]
        while pools and len(selected) < MAX_PER_DAY:
            for pool in pools[:]:
                if not pool:
                    pools.remove(pool)
                    continue
                it = pool.pop(0)
                if norm_title(it["title"])[:18] in seen:
                    continue
                seen.add(norm_title(it["title"])[:18])
                selected.append(it)
                if len(selected) >= MAX_PER_DAY:
                    break
        result[date_str] = sorted(selected, key=lambda x: -x["dt"].timestamp())
    return result


def to_json_item(it):
    return {
        "title": it["title"],
        "desc": truncate(it["desc"]) or it["title"],
        "link": it["link"],
        "source": it["source"],
        "category": detect_tag(it["title"], it["desc"]),
        "published": it["dt"].strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "time": it["dt"].strftime("%H:%M"),
    }


# ----------------------------------------------------------------------------
# 数据落盘（与已有归档合并）
# ----------------------------------------------------------------------------
def load_day(date_str):
    path = os.path.join(NEWS_DIR, date_str + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return None
    return None


def write_day(date_str, items, now):
    """合并已有数据（按 link 去重，保留历史条目）后写入"""
    existing = load_day(date_str)
    old_items = existing.get("items", []) if existing else []
    old_links = {it["link"] for it in old_items}

    merged = old_items + [to_json_item(it) for it in items
                          if it["link"] not in old_links]
    merged.sort(key=lambda x: x.get("published", ""), reverse=True)

    if len(merged) < MIN_ITEMS_TO_WRITE:
        return None

    payload = {
        "date": date_str,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "count": len(merged),
        "items": merged,
    }
    path = os.path.join(NEWS_DIR, date_str + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return len(merged)


def build_manifest(now):
    """扫描全部日期文件，生成 manifest 索引"""
    dates = []
    total_items = 0
    for fn in os.listdir(NEWS_DIR):
        if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", fn)):
            continue
        day = load_day(fn[:-5])
        if not day:
            continue
        items = day.get("items", [])
        if not items:
            continue
        cats = {}
        for it in items:
            cats[it["category"]] = cats.get(it["category"], 0) + 1
        dates.append({
            "date": day["date"],
            "count": len(items),
            "sources": len({it["source"] for it in items}),
            "categories": cats,
        })
        total_items += len(items)

    dates.sort(key=lambda d: d["date"], reverse=True)
    manifest = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "total_dates": len(dates),
        "total_items": total_items,
        "dates": dates,
    }
    path = os.path.join(DATA_DIR, "manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def run(days):
    now = datetime.now(TZ)
    os.makedirs(NEWS_DIR, exist_ok=True)
    log.info("AI 雷达 · 开始抓取 %s（回看 %d 天）",
             now.strftime("%Y-%m-%d %H:%M"), days)

    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_feed, name, url) for name, url in FEEDS]
        for fut in concurrent.futures.as_completed(futures):
            all_items.extend(fut.result())
    log.info("共抓取 %d 条原始条目", len(all_items))

    buckets = select_by_day(all_items, now, days)
    if not buckets:
        log.error("未筛选到任何资讯，请检查网络或 RSS 源可用性")
        return 1

    for date_str, items in sorted(buckets.items(), reverse=True):
        n = write_day(date_str, items, now)
        if n:
            log.info("写入 data/news/%s.json（%d 条）", date_str, n)

    manifest = build_manifest(now)
    log.info("manifest 更新完成：%d 天 / %d 条",
             manifest["total_dates"], manifest["total_items"])
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 雷达数据抓取器")
    parser.add_argument("--days", type=int, default=2,
                        help="回看时间窗口（天），首次播种建议 5")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(run(args.days))
