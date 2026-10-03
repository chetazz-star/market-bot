#!/usr/bin/env python3
"""Live market watcher that sends Telegram alerts.

Watches
  * Technicals: BTC/ETH on CLOSED candles - EMA 9/21, MACD, RSI (2-of-3 rule)
  * Fundamentals: Fear & Greed, global market cap, BTC dominance
  * Geopolitics / macro / crypto headlines via RSS, keyword filtered

First time (connects Telegram, no manual chat-id hunting)
  pip install -r requirements.txt
  python market_bot.py setup     # paste your @BotFather token, press Start in the bot

Then
  python market_bot.py --once    # one full cycle + summary, then exit (test)
  python market_bot.py           # runs forever

Alerts are information only, not financial advice.
"""
import calendar
import getpass
import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, stdev

import feedparser
import requests

# ----------------------------- settings ------------------------------------
ASSETS = {  # name: (Delta Exchange India symbol, CoinGecko id)
    "BTC": ("BTCUSD", "bitcoin"),
    "ETH": ("ETHUSD", "ethereum"),
}
TIMEFRAME = "4h"            # 1h, 4h or 1d (CoinGecko fallback always gives 4h)
TA_EVERY_MIN = 15
NEWS_EVERY_MIN = 5
SUMMARY_EVERY_HOURS = 6
COOLDOWN_MIN = 120          # min gap between signal alerts per asset
MAX_NEWS_PER_ALERT = 6

FEEDS = [  # (name, url, kind)
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml", "world"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml", "world"),
    ("Macro", "https://news.google.com/rss/search?q=Fed+OR+inflation+OR+tariffs"
              "+OR+oil+OR+sanctions&hl=en-IN&gl=IN&ceid=IN:en", "world"),
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", "crypto"),
    ("Cointelegraph", "https://cointelegraph.com/rss", "crypto"),
]

# Keyword filters (regex fragments, matched on whole words)
MACRO = ["wars?", "missiles?", "air ?strikes?", "invasion", "ceasefire", "sanctions?",
         "tariffs?", "embargo", "nuclear", "fed", "federal reserve", "rate (?:cut|hike)s?",
         "inflation", "recession", "opec", "oil prices?", "iran\\w*", "israel\\w*",
         "russia\\w*", "ukrain\\w*", "taiwan", "hormuz"]
CRYPTO = ["sec", "etfs?", "bans?", "banned", "hacks?", "hacked", "exploit\\w*",
          "liquidat\\w*", "crash\\w*", "plunge\\w*", "surge\\w*", "bankrupt\\w*",
          "lawsuit", "arrest\\w*", "stablecoins?", "rbi", "mica"]
RISK_OFF_W = ["wars?", "missiles?", "air ?strikes?", "invasion", "sanctions?", "tariffs?",
              "hacks?", "hacked", "exploit\\w*", "crash\\w*", "plunge\\w*", "bankrupt\\w*",
              "bans?", "banned", "arrest\\w*", "recession", "liquidat\\w*", "rate hikes?"]
RISK_ON_W = ["ceasefire", "rate cuts?", "etfs?", "surge\\w*"]


def rx(words):
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.I)


MACRO_RE, CRYPTO_RE = rx(MACRO), rx(CRYPTO + MACRO)
RISK_OFF, RISK_ON = rx(RISK_OFF_W), rx(RISK_ON_W)

# ----------------------------- plumbing ------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "bot_state.json")
CONFIG_FILE = os.path.join(HERE, "bot_config.json")
IST = timezone(timedelta(hours=5, minutes=30))
TF_SEC = {"1h": 3600, "4h": 14400, "1d": 86400}
DELTA_URL = "https://api.india.delta.exchange/v2/history/candles"
log = logging.getLogger("bot")


def load_config():
    """Env vars win; otherwise read the file written by `setup`."""
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            token, chat = token or cfg.get("token"), chat or cfg.get("chat_id")
        except Exception:
            pass
    return token, chat


TOKEN, CHAT_ID = load_config()


def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=15, headers={"User-Agent": "market-bot/1.0"})
    r.raise_for_status()
    return r.json()


def notify(text):
    log.info("ALERT\n%s", text)
    if not (TOKEN and CHAT_ID):
        return
    for i in range(0, len(text), 4000):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                              data={"chat_id": CHAT_ID, "text": text[i:i + 4000],
                                    "disable_web_page_preview": "true"}, timeout=15)
            if not r.ok:
                log.warning("Telegram error %s: %s", r.status_code, r.text[:200])
        except Exception as e:  # never log the exception text: it contains the token URL
            log.warning("Telegram send failed: %s", type(e).__name__)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


# ----------------------------- telegram setup ------------------------------
def tg(token, method, **params):
    """Call the Telegram Bot API. Raises RuntimeError without leaking the token."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/{method}", params=params, timeout=40)
        data = r.json()
    except Exception as e:
        raise RuntimeError(f"network error ({type(e).__name__})") from None
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram refused the request"))
    return data["result"]


def setup():
    print("Telegram setup\n"
          "1) In Telegram open @BotFather, send /newbot, choose a name and a username ending in 'bot'.\n"
          "2) Copy the token it gives you and paste it below (input is hidden).\n")
    token = getpass.getpass("Bot token: ").strip()
    try:
        me = tg(token, "getMe")
    except RuntimeError as e:
        sys.exit(f"Token rejected: {e}")
    user = me["username"]
    print(f"\nToken OK - bot is @{user}.")
    print(f"3) Open https://t.me/{user} in Telegram, press START and send 'hi'. Waiting up to 3 minutes...")
    chat_id, deadline, offset = None, time.time() + 180, None
    while time.time() < deadline and chat_id is None:
        try:
            params = {"timeout": 20, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            for u in tg(token, "getUpdates", **params):
                offset = u["update_id"] + 1
                msg = u.get("message")
                if msg and msg["chat"]["type"] == "private":
                    chat_id = msg["chat"]["id"]
                    break
        except RuntimeError as e:
            if "webhook" in str(e).lower():
                sys.exit("This bot has a webhook set. Use a fresh bot from @BotFather.")
            time.sleep(3)
    if chat_id is None:
        sys.exit("No message received. Press START in the bot chat, then run setup again.")
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"token": token, "chat_id": str(chat_id)}, f)
    try:
        tg(token, "sendMessage", chat_id=chat_id,
           text="Connected. Market bot alerts will arrive here. (Info only, not financial advice.)")
    except RuntimeError as e:
        sys.exit(f"Saved config but test message failed: {e}")
    print(f"\nConnected! Saved to {CONFIG_FILE} (owner-only). A test message was sent to your Telegram.")
    print("Next: python market_bot.py --once   then   python market_bot.py")


# ----------------------------- technicals ----------------------------------
def _delta(sym):
    end = int(time.time())
    rows = get_json(DELTA_URL, {"symbol": sym, "resolution": TIMEFRAME,
                                "start": end - TF_SEC[TIMEFRAME] * 300, "end": end})["result"]
    return [(float(r["time"]), float(r["close"])) for r in rows]


def _gecko(gid):
    rows = get_json(f"https://api.coingecko.com/api/v3/coins/{gid}/ohlc",
                    {"vs_currency": "usd", "days": 30})
    return [(r[0] / 1000, float(r[4])) for r in rows]


def get_closes(sym, gid):
    """Closed-candle closes, oldest first. Delta first, CoinGecko 4h as fallback."""
    for src, tf, fn, arg in (("Delta", TIMEFRAME, _delta, sym), ("CoinGecko", "4h", _gecko, gid)):
        try:
            closes = [c for _, c in sorted(fn(arg))]
            if len(closes) >= 60:
                return closes[:-1], src, tf  # drop the still-forming candle
            log.warning("%s returned only %d candles", src, len(closes))
        except Exception as e:
            log.warning("%s candles failed: %s", src, e)
    raise RuntimeError("no candle data")


def ema(values, n):
    k, out = 2 / (n + 1), [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, n=14):
    a, ag, al = 1 / n, None, None
    for prev, cur in zip(closes, closes[1:]):
        g, l = max(cur - prev, 0), max(prev - cur, 0)
        ag = g if ag is None else ag * (1 - a) + g * a
        al = l if al is None else al * (1 - a) + l * a
    if al == 0:
        return 50.0 if ag == 0 else 100.0
    return 100 - 100 / (1 + ag / al)


def analyze(c):
    e9, e21 = ema(c, 9), ema(c, 21)
    macd = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    msig = ema(macd, 9)
    r, price = rsi(c), c[-1]
    votes = {"EMA": 1 if e9[-1] > e21[-1] else -1,
             "MACD": 1 if macd[-1] > msig[-1] else -1,
             "RSI": 1 if r > 55 else -1 if r < 45 else 0}
    bulls = sum(v > 0 for v in votes.values())
    bears = sum(v < 0 for v in votes.values())
    gap_now, gap_prev = e9[-1] - e21[-1], e9[-2] - e21[-2]
    cross = ("fresh bullish EMA 9/21 cross" if gap_now > 0 >= gap_prev
             else "fresh bearish EMA 9/21 cross" if gap_now < 0 <= gap_prev else "")
    w = c[-20:]
    mid, sd = mean(w), stdev(w)
    bb = ("price above upper Bollinger band" if price > mid + 2 * sd
          else "price below lower Bollinger band" if price < mid - 2 * sd else "")
    return {"price": price, "rsi": r, "votes": votes, "cross": cross, "bb": bb,
            "signal": "BULLISH" if bulls >= 2 else "BEARISH" if bears >= 2 else "NEUTRAL"}


def check_ta(st):
    now = time.time()
    sigs, last_alert, snap = (st.setdefault(k, {}) for k in ("sig", "last_alert", "ta"))
    for name, (sym, gid) in ASSETS.items():
        try:
            closes, src, tf = get_closes(sym, gid)
            a = analyze(closes)
        except Exception as e:
            log.warning("TA failed for %s: %s", name, e)
            continue
        snap[name] = {"price": a["price"], "signal": a["signal"], "rsi": a["rsi"], "tf": tf}
        prev = sigs.get(name)
        if prev is None:  # first run: set baseline, no alert
            sigs[name] = a["signal"]
            continue
        if a["signal"] != prev and now - last_alert.get(name, 0) >= COOLDOWN_MIN * 60:
            word = {1: "bull", -1: "bear", 0: "flat"}
            txt = (f"{name} {tf}: {prev} -> {a['signal']}\n"
                   f"Price {a['price']:,.2f} | RSI {a['rsi']:.1f}\n"
                   + " | ".join(f"{k} {word[v]}" for k, v in a["votes"].items()))
            extra = [x for x in (a["cross"], a["bb"]) if x]
            if extra:
                txt += "\n" + "; ".join(extra)
            notify(txt + f"\n(closed candle, 2-of-3 rule, data: {src})")
            sigs[name], last_alert[name] = a["signal"], now


# ----------------------------- news ----------------------------------------
def fetch_feed(url):
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 market-bot"})
    r.raise_for_status()
    return feedparser.parse(r.content).entries


def check_news(st):
    seen_list = st.setdefault("seen", [])
    seen, primed = set(seen_list), st.setdefault("primed", [])
    hits = []
    for name, url, kind in FEEDS:
        try:
            entries = fetch_feed(url)
        except Exception as e:
            log.warning("feed %s failed: %s", name, e)
            continue
        if not entries:
            continue
        first = name not in primed  # first sight of a feed: mark as seen, don't alert
        pat = MACRO_RE if kind == "world" else CRYPTO_RE
        for e in entries[:40]:
            title, link = e.get("title", "").strip(), e.get("link", "")
            uid = hashlib.md5((link or title).encode()).hexdigest()[:16]
            if uid in seen:
                continue
            seen.add(uid)
            seen_list.append(uid)
            if first or not pat.search(title):
                continue
            pp = e.get("published_parsed")
            tag = " [risk-off?]" if RISK_OFF.search(title) else " [risk-on?]" if RISK_ON.search(title) else ""
            hits.append({"t": calendar.timegm(pp) if pp else time.time(),
                         "src": name, "title": title, "link": link, "tag": tag})
        if first:
            primed.append(name)
    del seen_list[:-4000]
    if not hits:
        return
    hits.sort(key=lambda h: h["t"])
    st.setdefault("recent", []).extend(hits)
    del st["recent"][:-30]
    body = "\n\n".join(f"[{h['src']}] {h['title']}{h['tag']}\n{h['link']}"
                       for h in hits[::-1][:MAX_NEWS_PER_ALERT])
    notify(f"Headline alert ({len(hits)} new)\n\n{body}")


# ----------------------------- fundamentals + summary ----------------------
def fundamentals():
    out = {}
    try:
        fg = get_json("https://api.alternative.me/fng/", {"limit": 1})["data"][0]
        out["fng"] = f"{fg['value']} ({fg['value_classification']})"
    except Exception as e:
        log.warning("fear&greed failed: %s", e)
    try:
        g = get_json("https://api.coingecko.com/api/v3/global")["data"]
        out.update(cap=g["total_market_cap"]["usd"],
                   chg=g["market_cap_change_percentage_24h_usd"],
                   dom=g["market_cap_percentage"]["btc"])
    except Exception as e:
        log.warning("global data failed: %s", e)
    return out


def send_summary(st):
    f = fundamentals()
    lines = [f"Market summary {datetime.now(IST):%d %b %H:%M} IST"]
    for name, s in st.get("ta", {}).items():
        lines.append(f"{name} {s['price']:,.2f} | {s['tf']} {s['signal']} | RSI {s['rsi']:.0f}")
    if "fng" in f:
        lines.append("Fear & Greed: " + f["fng"])
    if "cap" in f:
        lines.append(f"Global cap ${f['cap'] / 1e12:.2f}T ({f['chg']:+.1f}% 24h) | BTC dom {f['dom']:.1f}%")
    recent = [r for r in st.get("recent", []) if time.time() - r["t"] < 6 * 3600][-3:]
    if recent:
        lines.append("Flagged headlines (keyword tags only):")
        lines += [f"- [{r['src']}] {r['title']}{r['tag']}" for r in recent[::-1]]
    notify("\n".join(lines))


# ----------------------------- main loop -----------------------------------
def main():
    global CHAT_ID
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if "setup" in sys.argv[1:]:
        return setup()
    once = "--once" in sys.argv   # run every job now, then exit (test)
    tick = "--tick" in sys.argv   # run only jobs that are due, then exit (for GitHub Actions / cron)
    if not (TOKEN and CHAT_ID):
        log.warning("Telegram not connected - run `python market_bot.py setup`. "
                    "Alerts print to console only.")
    st = load_state()
    st.setdefault("t", {})
    if TOKEN and not CHAT_ID:  # no chat id given: find it from the message you sent to the bot
        CHAT_ID = st.get("chat_id")
        if not CHAT_ID:
            try:
                for u in reversed(tg(TOKEN, "getUpdates")):
                    m = u.get("message")
                    if m and m["chat"]["type"] == "private":
                        CHAT_ID = st["chat_id"] = str(m["chat"]["id"])
                        notify("Connected. Alerts will arrive here. (Info only, not financial advice.)")
                        break
            except RuntimeError as e:
                log.warning("could not look up chat id: %s", e)
            if not CHAT_ID:
                log.warning("No chat id yet: open your bot in Telegram, press Start, send 'hi', run again.")
    if not (once or tick):
        notify("Market bot started. Watching " + ", ".join(ASSETS) + " + news.")
    jobs = (("news", NEWS_EVERY_MIN, check_news), ("ta", TA_EVERY_MIN, check_ta),
            ("summary", SUMMARY_EVERY_HOURS * 60, send_summary))
    slack = 180 if tick else 0    # scheduled runs start a little early/late
    while True:
        for key, mins, fn in jobs:
            if once or time.time() - st["t"].get(key, 0) >= mins * 60 - slack:
                try:
                    fn(st)
                except Exception as e:
                    log.warning("%s failed: %s", key, e)
                st["t"][key] = time.time()
        save_state(st)
        if once or tick:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
