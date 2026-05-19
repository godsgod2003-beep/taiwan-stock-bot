#!/usr/bin/env python3
"""
主程式：同時執行互動 Bot（長輪詢）與每日晨報排程
- 互動 Bot：24/7 接收 Telegram 訊息，回應 AI 分析
- 晨報排程：週一至週五 08:30 台灣時間自動推播
"""

import threading
import time
import pytz
from datetime import datetime

import morning_report
import interactive_bot

TW = pytz.timezone("Asia/Taipei")


def schedule_loop():
    """每分鐘檢查是否到 08:30 台灣時間（週一至週五）"""
    sent_today = None
    while True:
        now = datetime.now(TW)
        today = now.date()
        # 週一=0 … 週五=4
        if now.weekday() < 5 and now.hour == 8 and now.minute == 30:
            if sent_today != today:
                sent_today = today
                print(f"[{now.strftime('%H:%M')}] 執行晨報...")
                try:
                    morning_report.main()
                except Exception as e:
                    print(f"  [晨報錯誤] {e}")
        time.sleep(30)  # 每 30 秒檢查一次，確保不錯過整點


if __name__ == "__main__":
    print(f"[{datetime.now(TW).strftime('%H:%M:%S')}] 啟動主程式（互動 Bot + 晨報排程）")

    # 晨報排程跑在背景執行緒
    t = threading.Thread(target=schedule_loop, daemon=True)
    t.start()

    # 互動 Bot 在主執行緒（長輪詢，阻塞）
    interactive_bot.poll_and_respond()
