#!/usr/bin/env python3
"""台股半導體晨報 - 每日自動推播至 Telegram"""

import os, sys, html, requests, pytz
import yfinance as yf
from datetime import datetime, timedelta

# ── 載入 .env ──────────────────────────────────────────────────────────────────
def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ── 設定 ───────────────────────────────────────────────────────────────────────
FUGLE_API_KEY    = os.getenv("FUGLE_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TW  = pytz.timezone("Asia/Taipei")
NOW = datetime.now(TW)

STOCKS = {
    "2330": "台積電",
    "2303": "聯電",
    "3711": "日月光",
    "6770": "力積電",
    "5347": "世界先進",
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


# ── 工具 ───────────────────────────────────────────────────────────────────────
def h(s):
    return html.escape(str(s))

def fmt_inst(n):
    if n == 0: return "—"
    sign = "+" if n > 0 else ""
    if abs(n) >= 1e8: return f"{sign}{n/1e8:.1f}億"
    if abs(n) >= 1e4: return f"{sign}{n/1e4:.0f}萬"
    return f"{sign}{n:.0f}"


# ── 資料取得 ───────────────────────────────────────────────────────────────────
def get_us_market():
    """昨夜美股（SOX / NASDAQ / S&P500 / 台積ADR）"""
    targets = {"^SOX": "SOX半導體", "^IXIC": "NASDAQ", "^GSPC": "S&P500", "TSM": "台積ADR"}
    out = {}
    for sym, name in targets.items():
        try:
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) >= 2:
                prev = float(hist["Close"].iloc[-2])
                last = float(hist["Close"].iloc[-1])
                out[name] = {"price": last, "pct": (last - prev) / prev * 100}
        except Exception:
            pass
    return out


def get_stock_quotes():
    """yfinance 台股報價 + MA5/MA20/MA60"""
    out = {}
    for code, name in STOCKS.items():
        try:
            suffix = ".TWO" if code == "5347" else ".TW"
            hist = yf.Ticker(f"{code}{suffix}").history(period="3mo")
            if hist.empty:
                continue
            c     = hist["Close"].tolist()
            last  = hist.iloc[-1]
            prev  = float(hist.iloc[-2]["Close"]) if len(hist) >= 2 else float(last["Close"])
            close = float(last["Close"])
            out[code] = {
                "close":      close,
                "change_pct": (close - prev) / prev * 100,
                "volume":     int(last["Volume"]),
                "ma5":        sum(c[-5:])  / min(5,  len(c)),
                "ma20":       sum(c[-20:]) / min(20, len(c)),
                "ma60":       sum(c[-60:]) / min(60, len(c)),
            }
        except Exception as e:
            print(f"  [yf] {code}: {e}")
    return out


def enrich_with_fugle(quotes):
    """用 Fugle API 補強數據（若填入 API key）"""
    if not FUGLE_API_KEY:
        return quotes
    try:
        from fugle_marketdata import RestClient
        client  = RestClient(api_key=FUGLE_API_KEY)
        from_dt = (NOW - timedelta(days=90)).strftime("%Y-%m-%d")
        to_dt   = (NOW - timedelta(days=1)).strftime("%Y-%m-%d")
        for code in STOCKS:
            try:
                resp = client.stock.historical.candles(**{"symbol": code, "from": from_dt, "to": to_dt})
                data = resp.get("data", [])
                if len(data) < 2:
                    continue
                c      = [d["close"] for d in data]
                latest = data[-1]
                prev   = data[-2]["close"]
                quotes.setdefault(code, {}).update({
                    "close":      latest["close"],
                    "change_pct": (latest["close"] - prev) / prev * 100,
                    "ma5":        sum(c[-5:])  / min(5,  len(c)),
                    "ma20":       sum(c[-20:]) / min(20, len(c)),
                    "ma60":       sum(c[-60:]) / min(60, len(c)),
                })
            except Exception as e:
                print(f"  [fugle] {code}: {e}")
    except ImportError:
        pass
    return quotes


def get_institutional():
    """TWSE 三大法人（往前找最近一個有效交易日）"""
    for delta in range(1, 6):
        dt = (NOW - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            r = requests.get(
                f"https://www.twse.com.tw/rwd/zh/fund/T86?date={dt}&selectType=ALLBUT0999&response=json",
                headers={"User-Agent": UA}, timeout=10
            )
            data = r.json()
            if data.get("stat") != "OK" or not data.get("data"):
                continue
            result = {}
            def to_int(s):
                try: return int(str(s).replace(",", "").replace(" ", "") or 0)
                except: return 0
            for row in data["data"]:
                code = row[0].strip()
                if code in STOCKS:
                    result[code] = {
                        "foreign": to_int(row[4]),
                        "trust":   to_int(row[10]),
                        "dealer":  to_int(row[16]),
                    }
            if result:
                date_str = f"{dt[:4]}/{dt[4:6]}/{dt[6:]}"
                return result, date_str
        except Exception as e:
            print(f"  [institutional] delta={delta}: {e}")
    return {}, ""


def get_news():
    """抓取半導體相關新聞（鉅亨 + Yahoo）"""
    news = []
    keywords = ["半導體","台積","聯電","日月光","晶圓","CoWoS","輝達","NVIDIA","力積","封裝","世界先進","AI晶片"]

    # 鉅亨網 API
    try:
        r = requests.get(
            "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=30",
            headers={"User-Agent": UA, "Referer": "https://www.cnyes.com/"}, timeout=10
        )
        for item in r.json().get("items", {}).get("data", []):
            title = item.get("title", "")
            if any(k in title for k in keywords):
                news.append({"title": title[:52], "src": "鉅亨"})
            if len(news) >= 4:
                break
    except Exception as e:
        print(f"  [cnyes] {e}")

    # Yahoo Finance（英文，TSMC ADR 相關）
    try:
        for item in (yf.Ticker("TSM").news or [])[:6]:
            title = item.get("title", "")
            if title and len(news) < 7:
                news.append({"title": title[:52], "src": "Yahoo"})
    except Exception as e:
        print(f"  [yahoo news] {e}")

    # 去重
    seen, unique = set(), []
    for n in news:
        key = n["title"][:20]
        if key not in seen:
            seen.add(key)
            unique.append(n)
    return unique[:6]


# ── 分析邏輯 ────────────────────────────────────────────────────────────────────
def tech_signal(q):
    close, ma5, ma20, ma60 = q["close"], q["ma5"], q["ma20"], q["ma60"]
    if   close > ma5 > ma20 > ma60: return "📈 強多頭"
    elif close > ma20 > ma60:       return "↗️ 偏多"
    elif close > ma60:              return "↔️ 整理"
    else:                           return "📉 弱勢"


def market_sentiment(us_data, inst):
    s = 50.0
    for name, w in [("SOX半導體", 2.5), ("NASDAQ", 1.5), ("台積ADR", 2.0), ("S&P500", 1.0)]:
        if name in us_data:
            s += us_data[name]["pct"] * w
    if inst:
        net = sum(v.get("foreign", 0) + v.get("trust", 0) for v in inst.values())
        s  += max(-12, min(12, net / 3e8))
    s = max(0, min(100, s))
    label = (
        "極度貪婪 🔥" if s >= 80 else
        "貪婪 😊"    if s >= 65 else
        "中性 😐"    if s >= 40 else
        "恐懼 😰"    if s >= 25 else
        "極度恐懼 💀"
    )
    return round(s), label


def gen_tips(quotes):
    tips = []
    for code, q in quotes.items():
        close, ma20, ma60 = q["close"], q["ma20"], q["ma60"]
        pct = q["change_pct"]

        if code == "2330":
            if close > 2310:
                tips.append(f"✅ 台積電：突破2,310，短線追進訊號")
            elif close > ma20:
                tips.append(f"✅ 台積電：站穩均線（MA20={ma20:.0f}），持倉不動")
            else:
                tips.append(f"⚠️ 台積電：跌破MA20（{ma20:.0f}），觀望")
        elif code == "2303":
            if pct > 5 or close > 105:
                tips.append(f"⏳ 聯電：漲幅過大，等回檔至90–95再建倉")
            elif close <= 95:
                tips.append(f"✅ 聯電：進入買入區（{close:.0f}），可分批建倉")
            else:
                tips.append(f"⏳ 聯電：勿追高，等回檔至90–95")
        elif code == "3711":
            if close > ma20:
                tips.append(f"✅ 日月光：均線多頭，目標650–700")
            else:
                tips.append(f"⚠️ 日月光：跌破MA20（{ma20:.0f}），待確認支撐")
        elif code == "6770":
            if close < ma60:
                tips.append(f"🚫 力積電：低於MA60（{ma60:.0f}），本週不操作")
            elif abs(close - ma60) / ma60 < 0.03:
                tips.append(f"⏳ 力積電：MA60附近（{ma60:.0f}），觀察站穩再進")
            else:
                tips.append(f"↔️ 力積電：中性觀察")
        elif code == "5347":
            if close > ma20:
                tips.append(f"✅ 世界先進：多頭排列，長期持有")
            else:
                tips.append(f"⚠️ 世界先進：跌破MA20（{ma20:.0f}），留意支撐")
    return tips


# ── 訊息組裝 ────────────────────────────────────────────────────────────────────
def build_html(us_data, quotes, inst, inst_date, news, s_val, s_lbl):
    L = []
    sep = "─" * 20

    L.append(f"🌅 <b>台股半導體晨報</b>  <code>{NOW.strftime('%m/%d %H:%M')}</code>")
    L.append(sep)

    # 美股
    L.append("<b>📊 昨夜美股</b>")
    for name, d in us_data.items():
        icon = "📈" if d["pct"] > 0 else "📉"
        L.append(f"  {icon} {h(name)}: <code>{d['pct']:+.2f}%</code>  ({d['price']:.1f})")
    L.append(sep)

    # 個股
    L.append("<b>🏭 目標個股（昨收）</b>")
    for code, q in quotes.items():
        i_data = inst.get(code, {})
        f_net  = i_data.get("foreign", 0)
        fi     = "🟢" if f_net > 0 else ("🔴" if f_net < 0 else "⚪")
        sig    = tech_signal(q)
        L.append(
            f"  <code>{code}</code> {h(STOCKS[code])}  "
            f"<b>${q['close']:.0f}</b> <code>{q['change_pct']:+.1f}%</code>  "
            f"外:{fi}<code>{h(fmt_inst(f_net))}</code>  {h(sig)}"
        )
    if inst_date:
        L.append(f"  <i>三大法人資料日期: {h(inst_date)}</i>")
    L.append(sep)

    # 新聞
    L.append("<b>📰 重點新聞</b>")
    if news:
        for i, n in enumerate(news, 1):
            L.append(f"  {i}. {h(n['title'])}  <i>[{h(n['src'])}]</i>")
    else:
        L.append("  （今日暫無符合關鍵字新聞）")
    L.append(sep)

    # 市場情緒
    L.append("<b>🌡 市場情緒</b>")
    bar = "█" * (s_val // 10) + "░" * (10 - s_val // 10)
    L.append(f"  {h(s_lbl)}  <code>{bar}</code> {s_val}/100")
    L.append(sep)

    # 操作提示
    L.append("<b>💡 今日操作提示</b>")
    for tip in gen_tips(quotes):
        L.append(f"  {h(tip)}")
    L.append(sep)

    L.append("<i>台股 09:00 開盤 · 僅供參考，請自行判斷風險</i>")
    return "\n".join(L)


# ── 推播 ────────────────────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  未設定 Telegram（請填寫 .env），僅顯示預覽")
        return False
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     msg,
            "parse_mode":               "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if r.status_code == 200:
        print("✅ 晨報推播成功")
        return True
    print(f"❌ 推播失敗 {r.status_code}: {r.text[:300]}")
    return False


# ── 主程式 ─────────────────────────────────────────────────────────────────────
def main():
    # 週末不執行
    if NOW.weekday() >= 5:
        print(f"[{NOW.strftime('%Y-%m-%d')}] 週末，跳過晨報")
        return 0
    print(f"[{NOW.strftime('%Y-%m-%d %H:%M:%S')}] === 台股半導體晨報 ===")

    print("→ 取得美股數據...")
    us_data = get_us_market()

    print("→ 取得台股報價（yfinance）...")
    quotes = get_stock_quotes()

    print("→ Fugle 補強（若有 API key）...")
    quotes = enrich_with_fugle(quotes)

    print("→ 取得三大法人（TWSE）...")
    inst, inst_date = get_institutional()

    print("→ 抓取新聞（鉅亨 + Yahoo）...")
    news = get_news()

    print("→ 計算市場情緒...")
    s_val, s_lbl = market_sentiment(us_data, inst)

    msg = build_html(us_data, quotes, inst, inst_date, news, s_val, s_lbl)

    print("\n── 訊息預覽 ──────────────────────────────")
    print(msg)
    print("──────────────────────────────────────────\n")

    send_telegram(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
