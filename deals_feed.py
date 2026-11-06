#!/usr/bin/env python3
"""Reads HotUKDeals' public RSS feed for the "lego" tag and sends new deals that matter to Jay's phone (ntfy).

Matches: sets on Jay's top 200 list, sets LEGO.com tags "Retiring soon", his themes, and posts about double points / gifts /
Black Friday. Remembers what it has already sent (deals_state.json). The first run only learns the feed (sends a short
"started" note). Nothing is bought or posted. Free: no accounts, no paid services.

Env: NTFY_TOPIC (secret), DEALS_DRY=1 prints only, DEALS_TEST=1 sends the newest few matching deals even if seen.
"""
import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

try:
    from curl_cffi import requests as cffi
except Exception:
    cffi = None
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get("DEALS_STATE") or os.path.join(HERE, "deals_state.json")   # the PC copy uses its own file
LABEL = os.environ.get("DEALS_LABEL", "")          # e.g. " (PC)" so Jay can tell which copy sent an alert
CA = os.environ.get("CURL_CA_BUNDLE") or True      # PC: trust AVG's HTTPS-scanning certificate
GAP = float(os.environ.get("DEALS_GAP", "0"))      # PC: seconds between stock page reads (polite)
VERY_EVERY_RUN = os.environ.get("DEALS_VERY_HOURLY") != "1"   # PC: read Very only on the first run of each hour
WATCH = os.path.join(HERE, "watch_sets.json")
FEEDS = ["https://www.hotukdeals.com/rss/tag/lego"]
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
DRY = os.environ.get("DEALS_DRY") == "1"
TEST = os.environ.get("DEALS_TEST") == "1"
MAX_LINES = 12

THEMES = re.compile(r"star wars|marvel|avengers|spider-?man|x-?men|harry potter|hogwarts|lord of the rings|one piece|"
                    r"\bf1\b|formula 1|speed champions|mandalorian|batman", re.I)
NEWS = re.compile(r"double points|triple points|insiders|gift with purchase|\bgwp\b|free gift|black friday|retiring|"
                  r"retired|last chance|price error|clearance", re.I)
NOISE = re.compile(r"\b(minifigure|keyring|key chain|lighting kit|light kit|led light|instruction)\b", re.I)


def fetch(url):
    headers = {"Accept-Language": "en-GB,en;q=0.9"}
    if cffi is not None:
        r = cffi.Session(impersonate="chrome").get(url, headers=headers, timeout=30, verify=CA)
    else:
        headers["User-Agent"] = "Mozilla/5.0"
        r = requests.get(url, headers=headers, timeout=30, verify=CA)
    if r.status_code != 200:
        raise RuntimeError("HTTP %d" % r.status_code)
    return r.text


def items_of(xml):
    root = ET.fromstring(xml)
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        merchant, price = "", ""
        for el in it:
            if el.tag.endswith("merchant"):
                merchant, price = el.get("name", ""), el.get("price", "")
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except Exception:
            ts = 0
        yield {"id": guid, "title": title, "link": link, "merchant": merchant, "price": price, "ts": ts}


def why(deal, watch):
    """Short reason this deal matters, or None."""
    nos = set(re.findall(r"(?<!\d)(\d{4,6})(?!\d)", deal["title"]))
    for no in nos:
        t = watch["top200"].get(no)
        if t:
            return "your list #%d" % t["rank"]
    for no in nos:
        if no in watch["retiring"]:
            return "retiring soon"
    if NEWS.search(deal["title"]):
        return "news/offer"
    if THEMES.search(deal["title"]) and not NOISE.search(deal["title"]):
        return "your theme"
    return None


def notify(title, body, priority="default"):
    if DRY or not TOPIC:
        print("[not sent]", title, "\n" + body)
        return
    r = requests.post("https://ntfy.sh/" + TOPIC, data=body.encode("utf-8"),
                      headers={"Title": (title + LABEL).encode("ascii", "ignore").decode(), "Priority": priority, "Tags": "moneybag"},
                      timeout=20)
    r.raise_for_status()


STOCKFILE = os.path.join(HERE, "stock_watch.json")        # [{"no": "76313", "name": "..."}]: sets Jay wants a restock alert for
VERY_CARD = re.compile(r'data-testid="gallery-product-card"[^>]*?data-cnstrc-item-name="([^"]*)"[^>]*?'
                       r'data-cnstrc-item-price="([\d.]+)"(.*?)(?=data-testid="gallery-product-card"|$)', re.S)


def lego_status(no):
    """(status, price) from LEGO.com's product page: InStock / OutOfStock / BackOrder / PreOrder ..."""
    t = fetch("https://www.lego.com/en-gb/product/" + no)
    a = re.search(r'"availability"\s*:\s*"https?://schema.org/(\w+)"', t)
    p = re.search(r'"price"\s*:\s*"?([\d.]+)', t)
    if not a:
        raise RuntimeError("no availability in page")
    return a.group(1), (p.group(1) if p else "")


def very_status(no):
    """('InStock', price) if Very's search page lists a card for the set without an out-of-stock note, else ('Absent', '')."""
    t = fetch("https://www.very.co.uk/e/q/lego+%s.end" % no)
    cards = VERY_CARD.findall(t)
    if not cards:
        raise RuntimeError("no product cards on Very page")
    for name, price, rest in cards:
        if re.search(r"(?<!\d)%s(?!\d)" % re.escape(no), name):
            txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", rest))
            if re.search(r"out of stock|sold out|currently unavailable|notify me", txt, re.I):
                return "OutOfStock", price
            return "InStock", price
    return "Absent", ""


def check_stock(state):
    """Restock alerts for the sets in stock_watch.json: LEGO.com and Very. Alerts on a change TO in stock / back order."""
    if not os.path.exists(STOCKFILE):
        return []
    sets = json.load(open(STOCKFILE, encoding="utf-8"))
    prev = state.setdefault("stock", {})
    alerts = []
    shops = [("LEGO.com", lego_status, "https://www.lego.com/en-gb/product/%s")]
    if VERY_EVERY_RUN or time.localtime().tm_min < 15:
        shops.append(("Very", very_status, "https://www.very.co.uk/e/q/lego+%s.end"))
    refused = set()
    for s in sets:
        for shop, fn, link in shops:
            key = "%s|%s" % (s["no"], shop)
            if shop in refused:
                continue
            if GAP:
                time.sleep(random.uniform(GAP, GAP * 2))
            try:
                status, price = fn(s["no"])
            except Exception as e:
                print("stock check failed %s: %s" % (key, str(e)[:80]))
                if "HTTP 403" in str(e) or "HTTP 503" in str(e) or "HTTP 429" in str(e):
                    refused.add(shop)            # stop asking a shop that is refusing us this run
                continue
            old = prev.get(key)
            prev[key] = status
            orderable = status in ("InStock", "BackOrder", "LimitedAvailability")
            if old is not None and old != status and orderable:
                alerts.append("BACK IN STOCK: %s %s at %s%s\n%s" % (s["name"], s["no"], shop,
                              (" £" + price) if price else "", link % s["no"]))
            print("stock", key, old, "->", status, price)
    return alerts


def main():
    watch = json.load(open(WATCH, encoding="utf-8"))
    state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {"seen": [], "started": False}
    seen = set(state.get("seen", []))
    deals = {}
    for url in FEEDS:
        for d in items_of(fetch(url)):
            deals[d["id"]] = d
    print("feed items:", len(deals), "already seen:", len(seen & set(deals)))
    newest_first = sorted(deals.values(), key=lambda d: -d["ts"])
    new = [d for d in newest_first if d["id"] not in seen]
    if not state.get("started") and not TEST:
        notify("LEGO deals feed started", "Watching HotUKDeals LEGO posts for your list, retiring sets and themes. "
               "You will only get new matching deals from now on.", "low")
        state["started"] = True
        new = []
    pool = newest_first if TEST else new
    hits = [(why(d, watch), d) for d in pool if why(d, watch)]
    if TEST:
        hits = hits[:5]
    if hits:
        lines = []
        for w, d in hits[:MAX_LINES]:
            shop = ("@ " + d["merchant"]) if d["merchant"] else ""
            lines.append("%s %s - %s [%s]\n%s" % (d["price"], shop, d["title"][:110], w, d["link"]))
        if len(hits) > MAX_LINES:
            lines.append("...and %d more on HotUKDeals" % (len(hits) - MAX_LINES))
        prio = "high" if any(w.startswith("your list") for w, _ in hits) else "default"
        notify("LEGO deals (%d new)" % len(hits), "\n\n".join(lines), prio)
    for a in check_stock(state):                 # restock alerts for the sets Jay is waiting for
        notify("Back in stock", a, "urgent")
    state["seen"] = list(set(deals) | seen)[-400:]
    if not DRY:
        json.dump(state, open(STATE, "w", encoding="utf-8"))
    print("new items:", len(new), "matches sent:", len(hits))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("deals feed failed:", type(e).__name__, str(e)[:120])
        sys.exit(1)
