# -*- coding: utf-8 -*-
"""
每日自动更新（GitHub Actions 专用，零第三方依赖）。
1) 刷新已有项目的星标 / issue / 协议 / 语言，并记录当日星标历史
2) 抓取近 7 天新开源项目，过滤黑名单与重复后入库
3) 回写 data/projects.json 与 data/star_history.json

用法：
  python3 tools/daily_update.py             # 正常执行
  python3 tools/daily_update.py --dry-run   # 只统计，不写文件
  python3 tools/daily_update.py --refresh-limit 20   # 只刷新前20个（测试用）
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")
HISTORY_FILE = os.path.join(DATA_DIR, "star_history.json")

API = "https://api.github.com"
UA = "Doubao-Digest-Bot"
TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""

STAR_MIN = 5
SUMMARY_MAX = 80
HISTORY_MAX_POINTS = 180
SEARCH_PAGES = 3
PER_PAGE = 50
NEW_WINDOW_DAYS = 7

# 经典库：高星、仍在维护的老项目（不占「每日新收录」名额，独立存 classic.json）
CLASSIC_FILE = os.path.join(DATA_DIR, "classic.json")
CLASSIC_STAR_MIN = 500
CLASSIC_WINDOW_DAYS = 7
CLASSIC_DAILY_MAX = 25
CLASSIC_TOTAL_MAX = 500
CLASSIC_PAGES = 1
CLASSIC_PER_KW_MAX = 2
CLASSIC_KEYWORDS = [
    "ai", "agent", "llm", "rag", "ollama", "player", "nas", "webdav", "sync",
    "backup", "design", "svg", "reverse", "hook", "firmware", "esp32",
    "emulator", "ocr", "pdf", "markdown", "docker", "terminal", "monitor",
    "automation", "self-hosted",
]
# HelloGitHub 参考通道：RSS 只给期号与分类统计，项目明细在期号页的 __NEXT_DATA__
HELLO_FILE = os.path.join(BASE_DIR, "hello.json")
HELLO_RSS = "https://hellogithub.com/rss"
HELLO_VOLUME = "https://hellogithub.com/periodical/volume/%d"
HELLO_DAILY_MAX = 5          # 每天最多从周刊补录几条（含在当日新收录总数里）
HELLO_STAR_MIN = 100         # 周刊项目的最低星标门槛（快照值）
HELLO_MAX_ISSUES = 2         # 单次最多补录的期数

# 说明：iptv / 影视资源导航 / 直播源聚合这类站点存在版权风险，不纳入自动抓取；
# 个别由用户指定的条目走 data/classic.json 里 user_pinned 的人工补录。

BLACKLIST = ["破解", "外挂", "作弊", "盗刷", "挖矿", "木马", "盗版", "付费解锁",
             "porn", "nsfw", "onlyfans", "keygen", "crack", "cheatengine"]

CATEGORY_RULES = [
    ("逆向开发", ["reverse", "逆向", "decompil", "frida", "ida", "firmware", "固件", "jailbreak", "越狱", "unlock", "漏洞", "exploit"]),
    ("硬件固件", ["arduino", "esp32", "esp8266", "stm32", "raspberry", "单片机", "嵌入式", "firmware", "iot", "蓝牙", "串口", "驱动"]),
    ("AI&Agent", ["llm", "gpt", "agent", "rag", "embedding", "diffusion", "transformer", "模型", "大模型", "智能体", "推理", "微调", "openai", "langchain"]),
    ("影音播放器", ["player", "播放器", "audio", "video", "音视频", "music", "音乐", "subtitle", "字幕", "ffmpeg", "stream"]),
    ("NAS存储", ["nas", "storage", "存储", "网盘", "backup", "备份", "sync", "同步", "s3", "cloud drive"]),
    ("游戏", ["game", "游戏", "emulator", "模拟器", "mod", "模组", "unity", "godot", "steam"]),
    ("设计", ["design", "设计", "figma", "svg", "draw", "绘图", "image", "图片", "ui kit", "photoshop", "canvas", "icon"]),
    ("系统工具", ["monitor", "监控", "system", "系统", "terminal", "终端", "shell", "cleaner", "清理", "driver", "docker", "kubernetes", "cli"]),
]
DEFAULT_CATEGORY = "效率工具"

PLATFORM_RULES = [
    ("Windows", ["windows", "win32", "wpf", "winform", "exe", "powershell"]),
    ("macOS", ["macos", "mac", "darwin", "homebrew", "ios", "iphone", "ipad"]),
    ("Android", ["android", "apk", "termux", "magisk", "xposed", "auto.js"]),
    ("Linux", ["linux", "ubuntu", "debian", "arch", "centos", "docker"]),
    ("Web", ["web", "browser", "chrome", "extension", "插件", "frontend", "react", "vue", "next.js"]),
    ("跨平台", ["cross-platform", "cross platform", "electron", "tauri", "flutter", "qt", "跨平台"]),
]


# ---------------- HTTP ----------------

def api_get(path, params=None):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": UA,
        "Authorization": "Bearer " + TOKEN,
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8")), dict(r.headers)


# ---------------- 规则 ----------------

def hit_blacklist(text):
    low = (text or "").lower()
    for w in BLACKLIST:
        wl = w.lower()
        if wl.isascii():
            if re.search(r"(?<![a-z0-9])" + re.escape(wl) + r"(?![a-z0-9])", low):
                return w
        elif wl in low:
            return w
    return ""


def classify(title, summary, topics):
    text = " ".join([title or "", summary or "", " ".join(topics or [])]).lower()
    for cat, words in CATEGORY_RULES:
        for w in words:
            wl = w.lower()
            if wl.isascii():
                # 英文词按词边界匹配（允许复数），避免 game 命中 endgame、ai 命中 email 这类误判
                if re.search(r"(?<![a-z0-9])" + re.escape(wl) + r"(s|es)?(?![a-z0-9])", text):
                    return cat
            elif wl in text:
                return cat
    return DEFAULT_CATEGORY


def detect_platforms(title, summary, topics):
    text = " ".join([title or "", summary or "", " ".join(topics or [])]).lower()
    out = []
    for name, words in PLATFORM_RULES:
        for w in words:
            if w in text:
                if name not in out:
                    out.append(name)
                break
    return out or ["跨平台"]


def clip(s, n=SUMMARY_MAX):
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n].rstrip() + "…"


# ---------------- 数据 ----------------

def hello_fetch(url, timeout=20):
    """访问 hellogithub.com（非 GitHub API，不带 Authorization）"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def hello_new_issue_repos():
    """RSS 发现期号 → 期号页取明细；只返回本地 hello.json 尚未覆盖的期里 star 达标的项目"""
    if not os.path.exists(HELLO_FILE):
        return []
    hello_doc = load_json(HELLO_FILE, {"rows": [], "meta": {}})
    covered = int((hello_doc.get("meta") or {}).get("issue_max") or 0)
    xml = hello_fetch(HELLO_RSS)
    issues = set()
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        t = re.search(r"<title>(.*?)</title>", m.group(1), re.S)
        if not t:
            continue
        n = re.search(r"第\s*(\d+)\s*期", t.group(1))
        if n:
            issues.add(int(n.group(1)))
    todo = sorted(x for x in issues if x > covered)[:HELLO_MAX_ISSUES]
    out = []
    for n in todo:
        try:
            html = hello_fetch(HELLO_VOLUME % n, timeout=25)
        except Exception:
            continue
        mm = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not mm:
            continue
        vol = json.loads(mm.group(1))["props"]["pageProps"]["volume"]
        for cat in vol.get("data") or []:
            for it in cat.get("items") or []:
                full = (it.get("full_name") or "").strip()
                if not full or int(it.get("stars") or 0) < HELLO_STAR_MIN:
                    continue
                out.append({"full": full, "issue": n})
        time.sleep(1)
    return out


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def to_item(repo):
    full = repo.get("full_name") or ""
    lic = repo.get("license") or {}
    return {
        "id": full.lower(),
        "gh_id": int(repo.get("id") or 0),
        "crawl_date": time.strftime("%Y-%m-%d"),
        "title": (repo.get("name") or full.split("/")[-1])[:60],
        "summary": clip(repo.get("description") or ""),
        "github_url": repo.get("html_url") or ("https://github.com/" + full),
        "star": int(repo.get("stargazers_count") or 0),
        "last_commit": (repo.get("pushed_at") or "")[:10] or time.strftime("%Y-%m-%d"),
        "category_tags": [classify(repo.get("name"), repo.get("description"), repo.get("topics"))],
        "platform_tags": detect_platforms(repo.get("name"), repo.get("description"), repo.get("topics")),
        "is_hidden": False,
        "license": lic.get("spdx_id") or "",
        "language": repo.get("language") or "",
        "open_issues": int(repo.get("open_issues_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "topics": repo.get("topics") or [],
        "homepage": (repo.get("homepage") or "").strip(),
        "repo_created": (repo.get("created_at") or "")[:10],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def update_existing(item, repo):
    lic = repo.get("license") or {}
    item["star"] = int(repo.get("stargazers_count") or 0)
    item["open_issues"] = int(repo.get("open_issues_count") or 0)
    item["forks"] = int(repo.get("forks_count") or 0)
    item["language"] = repo.get("language") or ""
    item["license"] = lic.get("spdx_id") or ""
    item["homepage"] = (repo.get("homepage") or "").strip()
    item["topics"] = repo.get("topics") or []
    item["last_commit"] = (repo.get("pushed_at") or "")[:10] or item.get("last_commit")


def full_name_of(item):
    url = item.get("github_url") or ""
    if "github.com/" not in url:
        return ""
    return url.split("github.com/", 1)[1].strip().rstrip("/")


# ---------------- 主流程 ----------------

def run(dry_run=False, refresh_limit=0):
    projects_doc = load_json(PROJECTS_FILE, {"projects": [], "meta": {}})
    projects = projects_doc.get("projects", [])
    hist_doc = load_json(HISTORY_FILE, {"history": {}})
    history = hist_doc.get("history", {})
    classic_doc = load_json(CLASSIC_FILE, {"items": [], "meta": {}})
    classics = classic_doc.get("items", [])

    report = {"date": time.strftime("%Y-%m-%d"), "existing": len(projects),
              "refreshed": 0, "refresh_errors": 0,
              "candidates": 0, "added": 0,
              "skipped_dup": 0, "skipped_star": 0, "skipped_blacklist": 0,
              "classic_total": len(classics), "classic_checked": 0, "classic_added": 0,
              "hello_candidates": 0, "hello_added": 0, "hello_dup": 0, "hello_failed": 0,
              "hello_note": None,
              "rate_remaining": None}

    # 1) 刷新已有项目
    targets = projects[:refresh_limit] if refresh_limit else projects
    for p in targets:
        full = full_name_of(p)
        if not full:
            continue
        try:
            repo, headers = api_get("/repos/" + full)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 仓库已被删除/封禁：自动隐藏，不再出现在收录库
                p["is_gone"] = True
                p["is_hidden"] = True
                report["gone"] = report.get("gone", 0) + 1
                continue
            report["refresh_errors"] += 1
            if e.code == 403:
                report["note"] = "触发限流，刷新中止"
                break
            continue
        except Exception:
            report["refresh_errors"] += 1
            continue
        p.pop("is_gone", None)
        update_existing(p, repo)
        report["refreshed"] += 1
        try:
            for k in ("X-RateLimit-Remaining", "x-ratelimit-remaining"):
                if k in headers:
                    report["rate_remaining"] = int(headers[k])
                    break
        except Exception:
            pass
        time.sleep(0.15)

    # 2) 抓新项目
    by_url = {(p.get("github_url") or "").rstrip("/").lower() for p in projects}
    by_gid = {int(p["gh_id"]) for p in projects if p.get("gh_id")}
    since = time.strftime("%Y-%m-%d",
                          time.localtime(time.time() - NEW_WINDOW_DAYS * 86400))
    query = "created:>=%s stars:>=%d fork:false archived:false" % (since, STAR_MIN)

    for page in range(1, SEARCH_PAGES + 1):
        try:
            data, headers = api_get("/search/repositories", {
                "q": query, "sort": "stars", "order": "desc",
                "per_page": PER_PAGE, "page": page})
        except urllib.error.HTTPError as e:
            report["note"] = "搜索被拒 HTTP %s（可能限流）" % e.code
            break
        except Exception as e:
            report["note"] = "搜索失败：%s" % e
            break

        items = data.get("items") or []
        report["candidates"] += len(items)
        for repo in items:
            full = repo.get("full_name") or ""
            url = (repo.get("html_url") or "").rstrip("/").lower()
            gid = int(repo.get("id") or 0)
            if url in by_url or (gid and gid in by_gid):
                report["skipped_dup"] += 1
                continue
            if int(repo.get("stargazers_count") or 0) < STAR_MIN:
                report["skipped_star"] += 1
                continue
            hit = hit_blacklist("%s %s" % (repo.get("name"), repo.get("description")))
            if hit:
                report["skipped_blacklist"] += 1
                continue
            item = to_item(repo)
            projects.append(item)
            by_url.add(url)
            if gid:
                by_gid.add(gid)
            report["added"] += 1
        if len(items) < PER_PAGE:
            break
        time.sleep(1)

    # 2b) 抓经典高星老项目（近期仍活跃，独立统计，不占每日新收录名额）
    in_main = {(p.get("github_url") or "").rstrip("/").lower() for p in projects}
    by_cid = {(c.get("github_url") or "").rstrip("/").lower(): c for c in classics}
    c_since = time.strftime("%Y-%m-%d",
                            time.localtime(time.time() - CLASSIC_WINDOW_DAYS * 86400))
    for kw in CLASSIC_KEYWORDS:
        if report["classic_added"] >= CLASSIC_DAILY_MAX:
            break
        q = "%s in:name,description,readme pushed:>=%s stars:>=%d fork:false archived:false" % (
            kw, c_since, CLASSIC_STAR_MIN)
        try:
            data, headers = api_get("/search/repositories", {
                "q": q, "sort": "stars", "order": "desc",
                "per_page": 30, "page": 1})
        except urllib.error.HTTPError as e:
            report["classic_note"] = "经典库搜索被拒 HTTP %s" % e.code
            break
        except Exception as e:
            report["classic_note"] = "经典库搜索失败：%s" % e
            break

        report["classic_checked"] += 1
        kw_added = 0
        for repo in data.get("items") or []:
            if kw_added >= CLASSIC_PER_KW_MAX:
                break
            url = (repo.get("html_url") or "").rstrip("/").lower()
            if not url:
                continue
            if url in by_cid:
                update_existing(by_cid[url], repo)
                by_cid[url]["classic"] = True
                continue
            if url in in_main:
                continue
            if int(repo.get("stargazers_count") or 0) < CLASSIC_STAR_MIN:
                continue
            if hit_blacklist("%s %s" % (repo.get("name"), repo.get("description"))):
                report["skipped_blacklist"] += 1
                continue
            if report["classic_added"] >= CLASSIC_DAILY_MAX:
                break
            item = to_item(repo)
            item["classic"] = True
            item["first_seen"] = item["crawl_date"]
            classics.append(item)
            by_cid[url] = item
            report["classic_added"] += 1
            kw_added += 1
        time.sleep(1)

    if len(classics) > CLASSIC_TOTAL_MAX:
        classics.sort(key=lambda x: int(x.get("star") or 0), reverse=True)
        del classics[CLASSIC_TOTAL_MAX:]
    report["classic_total"] = len(classics)

    # 2c) HelloGitHub 参考通道：周刊最新期的高星项目补进当日新收录
    if os.path.exists(HELLO_FILE):
        try:
            cand = hello_new_issue_repos()
        except Exception as e:
            report["hello_note"] = "参考源读取失败：%s" % str(e)[:80]
            cand = []
        report["hello_candidates"] = len(cand)
        have_ids = {(p.get("id") or "").lower() for p in projects}
        have_ids |= {(c.get("id") or "").lower() for c in classics}
        for c in cand:
            if report["hello_added"] >= HELLO_DAILY_MAX:
                break
            full = c["full"]
            if full.lower() in have_ids:
                report["hello_dup"] += 1
                continue
            try:
                repo, _ = api_get("/repos/" + full)
            except Exception:
                report["hello_failed"] += 1
                continue
            if int(repo.get("stargazers_count") or 0) < STAR_MIN:
                continue
            if hit_blacklist("%s %s" % (repo.get("name"), repo.get("description"))):
                report["skipped_blacklist"] += 1
                continue
            item = to_item(repo)
            item["from_hellogithub"] = c["issue"]
            projects.append(item)
            have_ids.add(full.lower())
            by_url.add((item.get("github_url") or "").rstrip("/").lower())
            report["added"] += 1
            report["hello_added"] += 1

    # 3) 记录星标历史
    day = time.strftime("%Y-%m-%d")
    for p in projects:
        key = p.get("id") or (p.get("github_url") or "").rstrip("/").lower()
        if not key:
            continue
        series = history.setdefault(key, [])
        point = {"date": day, "star": int(p.get("star") or 0)}
        if series and series[-1].get("date") == day:
            series[-1] = point
        else:
            series.append(point)
        if len(series) > HISTORY_MAX_POINTS:
            del series[:-HISTORY_MAX_POINTS]

    if dry_run:
        report["dry_run"] = True
        return report

    projects_doc["projects"] = projects
    projects_doc.setdefault("meta", {})["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_json(PROJECTS_FILE, projects_doc)
    classic_doc["items"] = classics
    cmeta = classic_doc.setdefault("meta", {})
    cmeta["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cmeta["total"] = len(classics)
    cmeta["daily_added"] = report["classic_added"]
    save_json(CLASSIC_FILE, classic_doc)
    save_json(HISTORY_FILE, {"history": history})
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-limit", type=int, default=0)
    args = ap.parse_args()
    if not TOKEN:
        print(json.dumps({"ok": False, "error": "缺少 GITHUB_TOKEN"}, ensure_ascii=False))
        return 1
    report = run(dry_run=args.dry_run, refresh_limit=args.refresh_limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
