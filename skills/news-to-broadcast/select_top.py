#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
select_top.py —— AI 情报站选题脚本（资讯转口播稿 skill 配套）

功能：
    1. 读取 data/news/YYYY-MM-DD.json（AI 情报站数据文件）
    2. 对每条 item 计算 4 维热度指标总分（关键词权重分 + 来源权重分 + 时效分 + 话题热度分）
    3. 按 4 条筛选标准过滤（强 AI 相关 / 有故事性 / 可拍性 / 信息密度）
    4. 输出 top 3 到 stdout，格式：标题 | 来源 | 热度分 | 筛选理由

仅使用 Python 标准库，无第三方依赖。

用法：
    python3 skills/news-to-broadcast/select_top.py [YYYY-MM-DD]

不传日期时，自动读取 data/manifest.json 取最新一天。
"""

import argparse
import datetime as dt
import json
import os
import re
import sys


# ============================== 路径配置 ==============================

# 脚本所在目录：skills/news-to-broadcast/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 项目根目录：脚本上溯两级
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))

# 数据目录
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
NEWS_DIR = os.path.join(DATA_DIR, "news")
MANIFEST_PATH = os.path.join(DATA_DIR, "manifest.json")


# ============================== 4 维热度指标 ==============================
# 维度①：关键词权重分（1-10，命中累加，封顶 10）

# 模型名
KEYWORD_MODEL = [
    "GPT", "GPT-4", "GPT-5", "o1", "o3", "Claude", "Gemini", "Sora",
    "Llama", "Qwen", "文心", "混元", "豆包", "RSI", "Flash",
    "DeepSeek", "ChatGPT", "扩散模型", "Diffusion", "Agent", "MCP", "RAG",
    "强化学习", "RLHF", "多模态", "大模型", "LLM", "VLM",
]

# 厂商名
KEYWORD_VENDOR = [
    "OpenAI", "Anthropic", "Google", "Meta", "Apple", "Microsoft", "英伟达",
    "NVIDIA", "字节", "阿里", "腾讯", "百度", "华为", "小米", "网易",
    "Snap", "Snapchat", "壁仞", "天数智芯", "沐曦", "摩尔线程", "燧原",
    "Google", "Anthropic", "OpenAI", "Google Home", "Claude",
]

# 融资 / 估值 / 资本动作
KEYWORD_FINANCE = [
    "融资", "估值", "亿美元", "万亿", "IPO", "申购", "拆分",
    "亿元", "美元", "上市", "半年报", "资本市场", "收购", "并购",
]

# 技术热点
KEYWORD_TECH = [
    "GPU", "芯片", "算力", "服务器", "M系列", "Ultra", "光模块", "互联",
    "数据中心", "AI基建", "机器人", "智能体", "AIAgent", "Agent",
]

KEYWORD_GROUPS = {
    "模型": KEYWORD_MODEL,
    "厂商": KEYWORD_VENDOR,
    "融资": KEYWORD_FINANCE,
    "技术": KEYWORD_TECH,
}


def score_keyword(item):
    """维度①：关键词权重分（1-10，命中累加封顶 10）。

    命中越多分组分越高，每命中一个关键词 +1，按命中分组数加权，
    最后封顶 10。
    """
    text = " ".join([
        item.get("title", ""),
        item.get("desc", ""),
        item.get("source", ""),
        item.get("category", ""),
    ]).lower()

    hit_groups = 0
    total_hits = 0
    for group_name, words in KEYWORD_GROUPS.items():
        group_hits = 0
        for w in words:
            if w.lower() in text:
                group_hits += 1
        if group_hits > 0:
            hit_groups += 1
            total_hits += group_hits

    # 每命中一个关键词 +1，命中分组数再加权（鼓励多维度命中），封顶 10
    score = total_hits + hit_groups
    return min(max(score, 1), 10), total_hits, hit_groups


# 维度②：来源权重分（1-5）

# AI 垂直媒体 = 5
SOURCE_AI_VERTICAL = {
    "量子位", "雷峰网", "机器之心", "36Kr AI", "Ars Technica",
    "The Verge AI", "TechCrunch AI", "AI News", "VentureBeat AI",
    "Ars Technica",
}
# 聚合媒体 = 1（早报 / 要闻汇总）
SOURCE_AGGREGATOR = {"雷峰网早报", "要闻汇总", "早报"}
# 判定为聚合媒体的标题关键词
AGGREGATOR_TITLE_HINTS = ["要闻提示", "早报", "日报", "一周"]


def score_source(item):
    """维度②：来源权重分（1-5）。

    AI 垂直媒体 > 通用科技媒体 > 聚合媒体。
    """
    source = item.get("source", "").strip()
    title = item.get("title", "")
    desc = item.get("desc", "")

    # 聚合媒体：早报 / 要闻汇总类
    if source in SOURCE_AGGREGATOR:
        return 1, "聚合媒体"
    if any(h in title for h in AGGREGATOR_TITLE_HINTS):
        return 1, "聚合媒体（标题含早报/要闻）"
    # 标题含多条要闻提示词，多半是早报
    if "要闻提示" in desc and ("1." in desc or "2." in desc):
        return 1, "聚合媒体（要闻提示）"

    # AI 垂直媒体
    if source in SOURCE_AI_VERTICAL:
        return 5, "AI 垂直媒体"

    # 通用科技媒体：含 AI 字样的也算较高
    if "ai" in source.lower():
        return 4, "含 AI 字样媒体"

    # 默认通用科技媒体
    return 3, "通用科技媒体"


# 维度③：时效分（当日 +10 / 昨日 +5 / 前日 +2 / 更早 0）

def _parse_published(item):
    """解析 published 字段为日期，失败返回 None。"""
    pub = item.get("published", "")
    if not pub:
        return None
    # 支持 2026-09-17T12:37:26+08:00
    try:
        return dt.datetime.fromisoformat(pub).date()
    except Exception:
        # 兜底：用正则提取 YYYY-MM-DD
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", pub)
        if m:
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                return None
        return None


def score_timeliness(item, target_date):
    """维度③：时效分。当日 +10，昨日 +5，前日 +2，更早 0。"""
    pub_date = _parse_published(item)
    if pub_date is None:
        # 无法解析时间，按当日处理（保守给 0，但通常 published 都有）
        return 0, "无时间，0分"

    delta = (target_date - pub_date).days
    if delta == 0:
        return 10, "当日 +10"
    elif delta == 1:
        return 5, "昨日 +5"
    elif delta == 2:
        return 2, "前日 +2"
    elif delta > 2:
        return 0, f"{delta}天前 0分"
    else:
        # delta < 0：发布日在目标日之后（数据异常或时区偏移），按当日
        return 10, "时间异常，按当日 +10"


# 维度④：话题热度分（1-8，命中累加封顶 8）

# 名人
TOPIC_PERSON = [
    "马斯克", "奥特曼", "Altman", "Sam", "周鸿祎", "周枫", "罗福莉",
    "谭平", "Tim", "Jensen", "黄仁勋",
]
# 大厂
TOPIC_BIGCOMPANY = [
    "苹果", "Apple", "谷歌", "Google", "微软", "Microsoft",
    "OpenAI", "Anthropic", "Meta", "字节", "小米", "网易", "Snap",
]
# 具体金额数字（正则匹配）
TOPIC_MONEY_PATTERN = re.compile(r"\d+(\.\d+)?\s*(亿美元|万亿|亿元|万美元|亿美金|美金)")
# 直播 / 现场事件
TOPIC_EVENT = [
    "直播", "发布会", "Open Day", "Disrupt", "ECCV", "现场",
    "首发", "官宣", "亮相", "开讲",
]
# 冲突 / 反转词
TOPIC_DRAMA = [
    "反打", "反攻", "翻车", "难哄", "沉寂", "官宣", "拼了", "烧",
    "暴跌", "暴涨", "退出", "拆分", "难哄", "惨遭", "可靠性翻车",
]


def score_topic(item):
    """维度④：话题热度分（1-8，命中累加封顶 8）。

    命中下列「戏剧性元素」即累加：名人 / 大厂 / 具体金额数字 / 直播现场 /
    冲突反转词。
    """
    text = " ".join([
        item.get("title", ""),
        item.get("desc", ""),
    ])

    hits = []
    # 名人
    for p in TOPIC_PERSON:
        if p in text:
            hits.append(f"名人:{p}")
            break  # 一类只算一次
    # 大厂
    for c in TOPIC_BIGCOMPANY:
        if c in text:
            hits.append(f"大厂:{c}")
            break
    # 具体金额数字
    if TOPIC_MONEY_PATTERN.search(text):
        m = TOPIC_MONEY_PATTERN.search(text).group(0)
        hits.append(f"金额:{m}")
    # 直播 / 现场事件
    for e in TOPIC_EVENT:
        if e in text:
            hits.append(f"事件:{e}")
            break
    # 冲突 / 反转词
    for d in TOPIC_DRAMA:
        if d in text:
            hits.append(f"反转:{d}")
            break

    score = len(hits) * 2  # 每命中一类 +2
    score = max(score, 1)  # 至少 1 分
    score = min(score, 8)  # 封顶 8
    return score, hits


# ============================== 综合打分 ==============================

def score_item(item, target_date):
    """对单条 item 计算 4 维热度总分，返回明细。"""
    kw_score, kw_hits, kw_groups = score_keyword(item)
    src_score, src_reason = score_source(item)
    tim_score, tim_reason = score_timeliness(item, target_date)
    top_score, top_hits = score_topic(item)

    total = kw_score + src_score + tim_score + top_score

    return {
        "title": item.get("title", ""),
        "source": item.get("source", ""),
        "link": item.get("link", ""),
        "category": item.get("category", ""),
        "score": total,
        "score_breakdown": {
            "keyword": kw_score,
            "source": src_score,
            "timeliness": tim_score,
            "topic": top_score,
        },
        "keyword_hits": kw_hits,
        "keyword_groups": kw_groups,
        "source_reason": src_reason,
        "timeliness_reason": tim_reason,
        "topic_hits": top_hits,
    }


# ============================== 4 条硬性筛选标准 ==============================

# 标准1：强 AI 相关
AI_RELEVANT_PATTERNS = [
    "AI", "人工智能", "大模型", "LLM", "GPT", "Claude", "Gemini", "Sora",
    "Anthropic", "OpenAI", "Agent", "智能体", "扩散模型", "强化学习",
    "GPU", "算力", "AIAgent", "MCP", "RAG", "VLM", "多模态",
]


def filter_ai_relevant(item, scored):
    """标准1：必须涉及 AI 技术 / 产品 / 公司 / 政策。"""
    text = " ".join([
        item.get("title", ""),
        item.get("desc", ""),
        item.get("category", ""),
        item.get("source", ""),
    ])
    for p in AI_RELEVANT_PATTERNS:
        if p in text:
            return True, f"命中AI关键词:{p}"
    return False, "未命中AI关键词"


def filter_story(scored):
    """标准2：有故事性 —— 能提炼出冲突 / 反转 / 悬念 / 具体数字 之一。"""
    title = scored["title"]
    # 冲突 / 反转词
    for d in TOPIC_DRAMA:
        if d in title:
            return True, f"故事性:{d}"
    # 具体数字
    if TOPIC_MONEY_PATTERN.search(title):
        return True, "故事性:具体金额"
    # 普通数字（非金额，但有数字也算）
    if re.search(r"\d+", title):
        return True, "故事性:含数字"
    # 名人 / 大厂出现在标题里也算有故事性
    for p in TOPIC_PERSON:
        if p in title:
            return True, f"故事性:名人{p}"
    for c in TOPIC_BIGCOMPANY:
        if c in title:
            return True, f"故事性:大厂{c}"
    # 标题里有问号 / 感叹号也算有悬念
    if "？" in title or "?" in title or "！" in title or "!" in title:
        return True, "故事性:悬念句"
    return False, "无故事性"


def filter_shootable(scored, item):
    """标准3：可拍性 —— 画面感强、有视觉元素、易于用 PPT + 口播呈现。"""
    text = " ".join([
        item.get("title", ""),
        item.get("desc", ""),
    ])
    visual_hints = [
        "直播", "现场", "Open Day", "发布会", "Disrupt", "ECCV",
        "画面", "曲线", "显卡", "服务器", "数据中心", "光模块", "光博会",
        "展示", "亮相", "首发", "Demo", "演示",
    ]
    for h in visual_hints:
        if h in text:
            return True, f"可拍:含视觉元素{h}"
    # 有金额 / 数字，可做数据大屏 PPT
    if TOPIC_MONEY_PATTERN.search(text):
        return True, "可拍:有金额数字"
    # 有名人 / 大厂，可放头像
    for p in TOPIC_PERSON:
        if p in text:
            return True, f"可拍:名人头像{p}"
    for c in TOPIC_BIGCOMPANY:
        if c in text:
            return True, f"可拍:大厂Logo{c}"
    return False, "无画面感"


def filter_density(scored, item):
    """标准4：信息密度 —— 单条资讯信息量足够支撑 600 字口播，不能太单薄。

    判断依据：desc 长度 + keyword 命中数 + topic 命中数综合评估。
    """
    desc = item.get("desc", "")
    title = item.get("title", "")
    text_len = len(desc) + len(title)

    # 信息密度综合分 = 文本长度 /30 + 关键词命中数 + 话题命中数
    density = text_len / 30.0 + scored["keyword_hits"] + len(scored["topic_hits"])

    if density >= 6:
        return True, f"信息密度高({density:.1f})"
    elif density >= 3:
        return True, f"信息密度中({density:.1f})"
    else:
        return False, f"信息密度低({density:.1f})，难以撑600字"


def apply_filters(item, scored):
    """对一条 item 应用 4 条硬筛，返回 (是否通过, 筛选理由)。"""
    reasons = []

    ok, r = filter_ai_relevant(item, scored)
    if not ok:
        return False, [f"标准1不过:{r}"]
    reasons.append(r)

    ok, r = filter_story(scored)
    if not ok:
        return False, [f"标准2不过:{r}"]
    reasons.append(r)

    ok, r = filter_shootable(scored, item)
    if not ok:
        return False, [f"标准3不过:{r}"]
    reasons.append(r)

    ok, r = filter_density(scored, item)
    if not ok:
        return False, [f"标准4不过:{r}"]
    reasons.append(r)

    return True, reasons


# ============================== 数据读取 ==============================

def load_latest_date():
    """从 manifest.json 读取最新一天日期（YYYY-MM-DD 字符串）。"""
    if not os.path.exists(MANIFEST_PATH):
        return None
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m = json.load(f)
        dates = m.get("dates", [])
        if not dates:
            return None
        # manifest 里 dates 按倒序，第一个就是最新
        return dates[0].get("date")
    except Exception:
        return None


def load_news(date_str):
    """读取 data/news/YYYY-MM-DD.json，返回 items 列表。"""
    path = os.path.join(NEWS_DIR, f"{date_str}.json")
    if not os.path.exists(path):
        return None, path
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("items", [])
        return items, path
    except Exception as e:
        print(f"[错误] 读取 {path} 失败: {e}", file=sys.stderr)
        return None, path


# ============================== 主流程 ==============================

def select_top(date_str, top_n=3):
    """对指定日期的新闻打分 + 筛选，返回 top N 的 scored 结果列表。"""
    items, path = load_news(date_str)
    if items is None:
        print(f"[错误] 找不到数据文件: {path}", file=sys.stderr)
        return []

    target_date = None
    try:
        target_date = dt.date.fromisoformat(date_str)
    except Exception:
        target_date = dt.date.today()

    # 打分
    scored_list = []
    for item in items:
        scored = score_item(item, target_date)
        scored_list.append((item, scored))

    # 排序：总分降序，同分按 ①>④>②>③
    def sort_key(pair):
        s = pair[1]
        return (
            -s["score"],
            -s["score_breakdown"]["keyword"],
            -s["score_breakdown"]["topic"],
            -s["score_breakdown"]["source"],
            -s["score_breakdown"]["timeliness"],
        )

    scored_list.sort(key=sort_key)

    # 筛选：按 4 条硬筛过滤，从高到低取前 N
    selected = []
    for item, scored in scored_list:
        if len(selected) >= top_n:
            break
        ok, reasons = apply_filters(item, scored)
        if ok:
            scored["filter_reason"] = "; ".join(reasons)
            selected.append((item, scored))

    # 如果硬筛后不足 N 条，从淘汰的里面按分数补足（但标注「未过全部硬筛」）
    if len(selected) < top_n:
        for item, scored in scored_list:
            if len(selected) >= top_n:
                break
            # 跳过已选
            if any(s is scored for _, s in selected):
                continue
            ok, reasons = apply_filters(item, scored)
            if not ok:
                scored["filter_reason"] = "未过全部硬筛(补足): " + "; ".join(reasons)
                selected.append((item, scored))

    return selected


def format_output(selected, date_str):
    """格式化为 stdout 输出：标题 | 来源 | 热度分 | 筛选理由"""
    lines = []
    lines.append(f"日期: {date_str}")
    lines.append(f"入选条数: {len(selected)}")
    lines.append("-" * 80)

    for i, (item, scored) in enumerate(selected, 1):
        title = scored["title"]
        source = scored["source"]
        score = scored["score"]
        reason = scored.get("filter_reason", "")

        # 明细
        bd = scored["score_breakdown"]
        breakdown_str = (
            f"关键词{bd['keyword']}/来源{bd['source']}"
            f"/时效{bd['timeliness']}/话题{bd['topic']}"
        )

        lines.append(f"[{i}] {title} | {source} | 热度分{score} | {reason}")
        lines.append(f"    明细: {breakdown_str}")
        lines.append(f"    话题命中: {', '.join(scored.get('topic_hits', [])) or '无'}")
        lines.append(f"    链接: {scored.get('link', '')}")
        lines.append("")

    lines.append("=" * 80)
    lines.append("输出格式：标题 | 来源 | 热度分 | 筛选理由")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="AI 情报站选题脚本：按 4 维热度打分 + 4 条筛选标准选 top 3"
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=None,
        help="目标日期 YYYY-MM-DD，不传则自动取 manifest 最新一天",
    )
    parser.add_argument(
        "-n", "--top",
        type=int,
        default=3,
        help="取前 N 条，默认 3",
    )
    args = parser.parse_args()

    # 确定日期
    date_str = args.date
    if date_str is None:
        date_str = load_latest_date()
    if date_str is None:
        date_str = dt.date.today().isoformat()
        print(f"[提示] 未指定日期且 manifest 读取失败，使用今天: {date_str}", file=sys.stderr)

    selected = select_top(date_str, top_n=args.top)

    if not selected:
        print(f"[提示] 日期 {date_str} 没有可选资讯，请检查数据文件。", file=sys.stderr)
        sys.exit(1)

    print(format_output(selected, date_str))


if __name__ == "__main__":
    main()
