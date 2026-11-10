#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HelloGitHub 月刊收录维护脚本

数据源：
  1) https://hellogithub.com/rss           —— 期号清单与发布日期（只含期号，不含项目明细）
  2) https://hellogithub.com/periodical/volume/{n} —— 期号详情页内嵌 __NEXT_DATA__，含每期全部项目

行为：
  - 比较 RSS 最新期号与 hello.json 的 meta.issue_max，仅抓取尚未收录的新期
  - 按仓库全名去重；已存在的条目只刷新 star / fork / 简介
  - 结果写回仓库根目录 hello.json（前端 digest.html 直接读取）

用法：
  python tools/hello_update.py            # 正常增量更新
  python tools/hello_update.py --dry      # 只打印统计，不写文件
  python tools/hello_update.py --issue 127 --dry   # 强制重抓指定期做校验
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELLO_FILE = os.path.join(ROOT, "hello.json")
RSS_URL = "https://hellogithub.com/rss"
VOLUME_URL = "https://hellogithub.com/periodical/volume/%d"
UA = "Mozilla/5.0 (compatible; githubpushu-hello-updater/1.0)"
MAX_ISSUES_PER_RUN = 5
SUMMARY_MAX = 60

COLS = ["id", "t", "s", "st", "f", "i", "d", "c"]


def http_get(url, timeout=25, retry=2):
    for i in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            if i == retry:
                raise
            time.sleep(2 + i * 3)
    return ""


def clip(s, n=SUMMARY_MAX):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def parse_rss():
    """返回 {期号: 发布日期}，只依赖 rss 的 item 标题与 pubDate"""
    xml = http_get(RSS_URL)
    issues = {}
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        t = re.search(r"<title>(.*?)</title>", block, re.S)
        p = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if not t:
            continue
        num = re.search(r"第\s*(\d+)\s*期", t.group(1))
        if not num:
            continue
        issues[int(num.group(1))] = (p.group(1).strip() if p else "")
    return issues


def parse_volume(issue, date_hint=""):
    """抓取期号详情页，解析 __NEXT_DATA__ 里的 volume.data"""
    html = http_get(VOLUME_URL % issue)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("期号页未找到 __NEXT_DATA__")
    data = json.loads(m.group(1))
    vol = data["props"]["pageProps"]["volume"]
    publish = str(vol.get("publish_at") or "")[:10] or date_hint
    items = []
    for cat in vol.get("data") or []:
        cname = cat.get("category_name") or "其它"
        if cname == "人工智能":
            cname = "AI&Agent"
        for it in cat.get("items") or []:
            full = (it.get("full_name") or "").strip()
            if not full:
                continue
            items.append([
                full,
                (it.get("name") or full.split("/")[-1]).strip(),
                clip(it.get("description")),
                int(it.get("stars") or 0),
                int(it.get("forks") or 0),
                issue,
                publish,
                cname,
            ])
    return items, publish


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="不写文件")
    ap.add_argument("--issue", type=int, default=0, help="强制重抓指定期号")
    args = ap.parse_args()

    if not os.path.exists(HELLO_FILE):
        print(json.dumps({"ok": False, "error": "hello.json 不存在"}, ensure_ascii=False))
        return 1

    payload = json.load(open(HELLO_FILE, encoding="utf-8"))
    rows = payload.get("rows") or []
    meta = payload.setdefault("meta", {})
    known = {r[0].lower(): i for i, r in enumerate(rows)}

    rss_issues = parse_rss()
    rss_max = max(rss_issues) if rss_issues else 0
    cur_max = int(meta.get("issue_max") or (max(r[5] for r in rows) if rows else 0))

    if args.issue:
        targets = [args.issue]
    else:
        targets = sorted(n for n in rss_issues if n > cur_max)[:MAX_ISSUES_PER_RUN]

    added, refreshed, failed = 0, 0, []
    for n in targets:
        try:
            items, publish = parse_volume(n, rss_issues.get(n, ""))
        except Exception as e:
            failed.append({"issue": n, "error": str(e)[:120]})
            continue
        for it in items:
            k = it[0].lower()
            if k in known:
                idx = known[k]
                rows[idx][3] = it[3]          # star 刷新
                rows[idx][4] = it[4]          # fork 刷新
                if not rows[idx][2]:
                    rows[idx][2] = it[2]
                refreshed += 1
            else:
                known[k] = len(rows)
                rows.append(it)
                added += 1
        time.sleep(1.2)   # 保持对站点温和

    if rows:
        meta["total"] = len(rows)
        meta["issue_min"] = min(r[5] for r in rows)
        meta["issue_max"] = max(r[5] for r in rows)
        meta["issue_count"] = len({r[5] for r in rows})
    meta["source"] = "HelloGitHub 月刊"
    meta["rss"] = RSS_URL
    meta["rss_issue_max"] = rss_max
    meta["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    meta["star_note"] = "星标为 HelloGitHub 站点快照，收录时未逐条回查"

    report = {
        "ok": True,
        "rss_max_issue": rss_max,
        "local_max_issue": cur_max,
        "fetched_issues": targets,
        "added": added,
        "refreshed": refreshed,
        "failed": failed,
        "total": len(rows),
        "issue_max": meta.get("issue_max"),
        "dry_run": bool(args.dry),
    }

    if not args.dry and (added or refreshed):
        payload["rows"] = rows
        payload["cols"] = COLS
        txt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        tmp = HELLO_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(txt)
        os.replace(tmp, HELLO_FILE)
        report["file_bytes"] = len(txt.encode("utf-8"))

    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
