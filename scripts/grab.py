# -*- coding: utf-8 -*-
"""官网直播流自动抓取（Playwright）+ 出流校验
思路：真浏览器打开直播页 -> 监听所有网络请求/响应 -> 抓 m3u8/flv
      -> 拉 m3u8 清单 -> 取首分片下载 -> 检查 TS 同步字节 0x47 或 fMP4
输出：result.json（candidates 全量候选 / verified 通过校验的）
"""
import json, os, time
from urllib.parse import urljoin
import requests
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SKIP_HOST = ["googletagmanager", "google-analytics", "doubleclick", "cnzz",
             "umeng", "baidu.com", "hm.baidu", "bdimg"]


def is_stream(u):
    ul = u.lower()
    if ".m3u8" in ul or ".flv" in ul:
        return True
    return False


def check_m3u8(url, referer, timeout=12):
    """返回 (ok, info)"""
    h = {"User-Agent": UA, "Referer": referer or url}
    try:
        r = requests.get(url, headers=h, timeout=timeout)
    except Exception as e:
        return False, "请求失败 %s" % type(e).__name__
    if r.status_code != 200:
        return False, "HTTP %d" % r.status_code
    txt = r.text
    if "#EXTM3U" not in txt:
        return False, "不是 m3u8（%d B）" % len(r.content)
    segs = [l.strip() for l in txt.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        # 可能是 master playlist
        subs = [l for l in txt.splitlines() if l.strip() and not l.startswith("#")]
        if subs:
            sub = urljoin(url, subs[0])
            try:
                r2 = requests.get(sub, headers=h, timeout=timeout)
                segs = [l.strip() for l in r2.text.splitlines()
                        if l.strip() and not l.startswith("#")]
                url = sub
            except Exception:
                return False, "子清单拉取失败"
        if not segs:
            return False, "清单无分片"
    seg = urljoin(url, segs[0])
    try:
        rs = requests.get(seg, headers=h, timeout=timeout)
    except Exception as e:
        return False, "分片请求失败 %s" % type(e).__name__
    if rs.status_code != 200 or len(rs.content) < 1000:
        return False, "分片异常 HTTP %d / %d B" % (rs.status_code, len(rs.content))
    b = rs.content
    ts_sync = b[0] == 0x47
    fmp4 = (b[4:8] == b"ftyp") or (b"moof" in b[:512])
    if not (ts_sync or fmp4):
        return False, "分片不是视频（首字节 %02x）" % b[0]
    return True, "%s %d KB" % ("TS" if ts_sync else "fMP4", len(b) // 1024)


def grab_page(browser, url, wait_ms):
    ctx = browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 768})
    page = ctx.new_page()
    seen = []

    def on_resp(resp):
        u = resp.url
        if is_stream(u) and not any(s in u for s in SKIP_HOST):
            seen.append(u)

    page.on("response", on_resp)
    page.on("request", lambda r: is_stream(r.url) and not any(s in r.url for s in SKIP_HOST) and seen.append(r.url))
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print("  [goto] %s" % e)
    page.wait_for_timeout(3000)
    # 尝试点一下播放：video 元素 / 含「播放」的按钮
    for sel in ["video", "[class*=play] svg", "[class*=player]", "text=播放"]:
        try:
            el = page.query_selector(sel)
            if el:
                el.click(timeout=2500)
                page.wait_for_timeout(1500)
                break
        except Exception:
            pass
    page.wait_for_timeout(wait_ms)
    title = page.title()
    # 若还没抓到，尝试成对地从频道列表里点进第一个频道
    if not seen:
        for sel in ["[class*=channel] li", "[class*=channelItem]", "a[href*=liveTV]"]:
            try:
                els = page.query_selector_all(sel)
                if els:
                    els[0].click(timeout=2500)
                    page.wait_for_timeout(wait_ms)
                    break
            except Exception:
                pass
    ctx.close()
    return title, seen


def main():
    cfg = json.load(open(os.path.join(ROOT, "sites.json"), encoding="utf-8"))
    result = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "sites": [], "verified": [], "candidates": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        for site in cfg.get("sites", []):
            print("══ %s" % site["name"])
            entry = {"name": site["name"], "pages": [], "streams": []}
            found = set()
            for u in site.get("pages", []):
                print("  → %s" % u)
                title, seen = grab_page(browser, u, site.get("wait_ms", 12000))
                uniq = [x for x in dict.fromkeys(seen)]
                print("    title=%s  抓到的流=%d" % (title, len(uniq)))
                entry["pages"].append({"url": u, "title": title, "hits": uniq})
                for x in uniq:
                    found.add(x)
            for u in sorted(found):
                ok, info = check_m3u8(u, site.get("home"))
                print("    校验 %-6s %s  %s" % ("OK" if ok else "FAI 
