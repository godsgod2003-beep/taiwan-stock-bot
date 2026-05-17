#!/usr/bin/env python3
"""
台股互動分析 Bot
- 輸入股票名稱或代號 → Gemini AI 給出當沖/波段建議
- 支援自然語言：「中探針今天可否入場」「2330 波段分析」
"""

import os, re, html, requests, pytz, json
import yfinance as yf
from datetime import datetime, timedelta

# ── 載入環境變數 ────────────────────────────────────────────────────────────────
def _load_env(path):
    if not os.path.exists(path): return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))

_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY", "")
TW = pytz.timezone("Asia/Taipei")

# ── 台股名稱→代號對照（常用股，可自行擴充）──────────────────────────────────────
STOCK_ALIAS = {
    "台積電": "2330", "台積": "2330", "tsmc": "2330",
    "聯電": "2303", "聯發科": "2454", "聯發": "2454",
    "日月光": "3711", "日月光投控": "3711",
    "力積電": "6770", "世界先進": "5347",
    "鴻海": "2317", "富士康": "2317",
    "中探針": "3512", "探針": "3512",
    "台達電": "2308", "台達": "2308",
    "廣達": "2382", "緯創": "3231",
    "華碩": "2357", "宏碁": "2353",
    "中鋼": "2002", "台塑": "1301",
    "國泰金": "2882", "富邦金": "2881", "中信金": "2891",
    "台灣大": "3045", "中華電": "2412",
    "京元電子": "2449", "京元電": "2449",
    "矽力": "6415", "矽力-KY": "6415",
    "創意": "3443", "創意電子": "3443",
    "瑞昱": "2379", "立積": "4968",
    "欣興": "3037", "臻鼎": "4958",
    "南亞科": "2408", "華邦電": "2344",
}

OTC_STOCKS = {"5347", "3512", "6415", "3443", "4968"}  # 上櫃代號

def resolve_stock(text: str) -> tuple[str, str] | None:
    """從文字中解析股票代號與名稱"""
    text = text.strip()
    # 直接輸入代號 (4~5位數字)
    m = re.search(r'\b(\d{4,5})\b', text)
    if m:
        code = m.group(1)
        name = next((k for k, v in STOCK_ALIAS.items() if v == code), code)
        return code, name
    # 名稱對照
    text_lower = text.lower()
    for alias, code in STOCK_ALIAS.items():
        if alias.lower() in text_lower:
            return code, alias
    return None


def get_stock_data(code: str) -> dict | None:
    """取得股票技術數據"""
    suffix = ".TWO" if code in OTC_STOCKS else ".TW"
    try:
        hist = yf.Ticker(f"{code}{suffix}").history(period="3mo")
        if hist.empty:
            return None
        c = hist["Close"].tolist()
        v = hist["Volume"].tolist()
        latest = hist.iloc[-1]
        prev   = hist.iloc[-2] if len(hist) >= 2 else latest
        close  = float(latest["Close"])
        prev_c = float(prev["Close"])

        # RSI(14)
        deltas = [c[i] - c[i-1] for i in range(1, len(c))]
        gains  = [d if d > 0 else 0 for d in deltas[-14:]]
        losses = [-d if d < 0 else 0 for d in deltas[-14:]]
        avg_gain = sum(gains) / 14 if gains else 0
        avg_loss = sum(losses) / 14 if losses else 0.0001
        rsi = 100 - (100 / (1 + avg_gain / avg_loss))

        ma5  = sum(c[-5:])  / min(5,  len(c))
        ma10 = sum(c[-10:]) / min(10, len(c))
        ma20 = sum(c[-20:]) / min(20, len(c))
        ma60 = sum(c[-60:]) / min(60, len(c))
        avg_vol = sum(v[-20:]) / min(20, len(v))
        today_vol = float(latest["Volume"])

        return {
            "close":      close,
            "change_pct": (close - prev_c) / prev_c * 100,
            "rsi":        round(rsi, 1),
            "ma5": round(ma5,1), "ma10": round(ma10,1),
            "ma20": round(ma20,1), "ma60": round(ma60,1),
            "vol_ratio":  round(today_vol / avg_vol, 2) if avg_vol else 1,
            "high_52w":   float(hist["Close"].max()),
            "low_52w":    float(hist["Close"].min()),
        }
    except Exception as e:
        print(f"  [yf] {code}: {e}")
        return None


def get_twse_institutional(code: str) -> dict:
    """TWSE 三大法人近日數據"""
    for delta in range(1, 5):
        dt = (datetime.now(TW) - timedelta(days=delta)).strftime("%Y%m%d")
        try:
            r = requests.get(
                f"https://www.twse.com.tw/rwd/zh/fund/T86?date={dt}&selectType=ALLBUT0999&response=json",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=8
            )
            data = r.json()
            if data.get("stat") != "OK": continue
            for row in data.get("data", []):
                if row[0].strip() == code:
                    def ti(s):
                        try: return int(str(s).replace(",","") or 0)
                        except: return 0
                    return {"foreign": ti(row[4]), "trust": ti(row[10]), "dealer": ti(row[16])}
        except Exception:
            pass
    return {}


def call_gemini(prompt: str) -> str:
    """呼叫 Gemini API 取得 AI 分析"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    r = requests.post(url, json=body, timeout=20)
    if r.status_code == 200:
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return f"Gemini 回應錯誤: {r.status_code}"


def analyze_stock(code: str, name: str, query: str) -> str:
    """完整股票分析流程"""
    data = get_stock_data(code)
    if not data:
        return f"❌ 找不到 {name}（{code}）的數據，請確認代號是否正確"

    inst = get_twse_institutional(code)
    now  = datetime.now(TW)

    # 判斷查詢意圖
    is_daytrade = any(w in query for w in ["當沖", "當天", "今天", "日內", "短線"])
    is_swing    = any(w in query for w in ["波段", "中線", "持股", "持有", "週線"])
    trade_type  = "當沖" if is_daytrade else ("波段" if is_swing else "當沖與波段")

    # 技術面快速判斷
    close, ma20, ma60 = data["close"], data["ma20"], data["ma60"]
    rsi = data["rsi"]
    trend = "多頭" if close > data["ma5"] > ma20 > ma60 else \
            "偏多" if close > ma20 else \
            "盤整" if close > ma60 else "弱勢"

    # 建構 Gemini 提示
    inst_str = f"外資{inst.get('foreign',0)/1000:.0f}千股，投信{inst.get('trust',0)/1000:.0f}千股" if inst else "無三大法人數據"

    prompt = f"""你是台股資深技術分析師，請用繁體中文給出簡潔的{trade_type}操作建議。

【個股資訊】
股票：{name}（{code}），分析時間：{now.strftime('%Y-%m-%d')}

【技術指標】
現價：${close:.1f}（{data['change_pct']:+.1f}%）
RSI(14)：{rsi}
均線：MA5={data['ma5']} MA10={data['ma10']} MA20={data['ma20']} MA60={data['ma60']}
均線趨勢：{trend}
量比（今日vs20日均）：{data['vol_ratio']}x
52週高/低：${data['high_52w']:.1f} / ${data['low_52w']:.1f}
三大法人：{inst_str}

【使用者問題】{query}

請依序回答（每項1-2句，簡潔有力）：
1. 🎯 今日操作方向（偏多/偏空/觀望）
2. ⚡ {'當沖建議（進場時機、目標、停損）' if is_daytrade or not is_swing else ''}{'📅 波段建議（進場區、目標、停損、持有期）' if is_swing or not is_daytrade else ''}
3. ⚠️ 主要風險（1句）
4. 💡 關鍵觀察點（1句）

回答要具體，給出價格數字，不要說「需要更多資訊」。"""

    ai_response = call_gemini(prompt)

    # 組裝最終訊息
    rsi_label = "超買⚠️" if rsi > 70 else "超賣⚠️" if rsi < 30 else "中性"
    vol_label = f"{data['vol_ratio']}x {'🔥大量' if data['vol_ratio'] > 1.5 else '正常'}"

    msg = [
        f"🔍 <b>{html.escape(name)}（{code}）</b>  {now.strftime('%m/%d')}",
        "─" * 20,
        f"💰 現價 <b>${close:.0f}</b>  <code>{data['change_pct']:+.1f}%</code>",
        f"📊 RSI: <code>{rsi}</code> {rsi_label}  量比: <code>{vol_label}</code>",
        f"📈 均線: MA20={ma20:.0f}  MA60={ma60:.0f}  趨勢: {html.escape(trend)}",
        "─" * 20,
        f"<b>🤖 AI 分析（{html.escape(trade_type)}）</b>",
        html.escape(ai_response.strip()),
        "─" * 20,
        "<i>⚠️ 僅供參考，投資有風險，請自行判斷</i>"
    ]
    return "\n".join(msg)


def send_telegram(chat_id: str, msg: str):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=15
    )


def poll_and_respond():
    """輪詢 Telegram 新訊息並回應"""
    offset = None
    print(f"[{datetime.now(TW).strftime('%H:%M:%S')}] 互動 Bot 啟動，等待訊息...")

    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset: params["offset"] = offset
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params=params, timeout=35
            )
            updates = r.json().get("result", [])

            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if not text or not chat_id:
                    continue

                # /start 指令
                if text == "/start":
                    send_telegram(chat_id,
                        "👋 <b>台股分析 Bot</b>\n\n"
                        "直接輸入股票名稱或代號即可分析！\n\n"
                        "範例：\n"
                        "• <code>中探針今天可否當沖？</code>\n"
                        "• <code>2330 波段分析</code>\n"
                        "• <code>台積電可以入場嗎</code>\n"
                        "• <code>聯電當沖</code>"
                    )
                    continue

                # 解析股票
                result = resolve_stock(text)
                if not result:
                    send_telegram(chat_id,
                        "❓ 找不到對應股票，請輸入股票名稱或4位代號\n"
                        "例如：<code>台積電</code> 或 <code>2330</code>"
                    )
                    continue

                code, name = result
                print(f"  → 查詢 {name}({code})：{text}")
                send_telegram(chat_id, f"⏳ 正在分析 <b>{html.escape(name)}（{code}）</b>，請稍候...")

                reply = analyze_stock(code, name, text)
                send_telegram(chat_id, reply)

        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"  [error] {e}")


if __name__ == "__main__":
    poll_and_respond()
