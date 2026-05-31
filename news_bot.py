import os
os.environ["YF_NO_CURL_CFFI"] = "1"  # Fixes the TLS / curl_cffi bug

import requests
import yfinance as yf
from datetime import datetime
from apscheduler.schedulers.blocking import BlockingScheduler

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

def get_global_news():
    url = f"https://newsapi.org/v2/top-headlines?source=google-news&language=en&apiKey={NEWS_API_KEY}"
    try:
        response = requests.get(url).json()
        articles = response.get("articles", [])[:5]
        news_str = "🌍 *Top Global News:*\n"
        for i, art in enumerate(articles, 1):
            news_str += f"{i}. [{art['title']}]({art['url']})\n"
        return news_str + "\n"
    except Exception as e:
        return f"🌍 *Top Global News:* Failed to fetch ({e})\n\n"

def get_business_and_stocks():
    try:
        market = yf.Ticker("SPY")
        yf_news = market.news[:10]
        biz_str = "💼 *Top 10 Business Stories:*\n"
        for i, story in enumerate(yf_news, 1):
            biz_str += f"{i}. [{story['title']}]({story['link']})\n"
    except Exception as e:
        biz_str = f"💼 *Top 10 Business Stories:* Failed to fetch ({e})\n"

    try:
        tickers = {"S&P 500": "^GSPC", "Nasdaq": "^IXIC", "OBX Index": "^OBX"} # Added Oslo OBX index!
        stock_str = "📈 *Market Watch:*\n"
        for name, sym in tickers.items():
            t = yf.Ticker(sym)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                close_today = hist['Close'].iloc[-1]
                close_prev = hist['Close'].iloc[-2]
                pct_change = ((close_today - close_prev) / close_prev) * 100
                sign = "+" if pct_change >= 0 else ""
                stock_str += f"• {name}: {close_today:,.2f} ({sign}{pct_change:.2f}%)\n"
            else:
                stock_str += f"• {name}: Data temporarily unavailable\n"
    except Exception:
        stock_str = ""

    return biz_str + "\n" + stock_str

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload)
        print(f"Telegram Response Status: {res.status_code}")
    except Exception as e:
        print(f"Failed to push message: {e}")

def job_daily_briefing():
    print(f"Executing briefing at {datetime.now()}")
    global_news = get_global_news()
    business_news = get_business_and_stocks()
    
    greeting = f"📅 *Good Morning! Your Briefing for {datetime.today().strftime('%Y-%m-%d')}*\n\n"
    full_message = greeting + global_news + business_news
    
    send_telegram_message(full_message)

if __name__ == "__main__":
    # Tell APScheduler to inherit the Docker system timezone ('Europe/Oslo')
    scheduler = BlockingScheduler(timezone="Europe/Oslo")
    
    # Schedule daily at 07:30 AM local Oslo time
    scheduler.add_job(job_daily_briefing, 'cron', hour=7, minute=30)
    
    print("--------------------------------------------------")
    print("⚡ RUNNING AN INSTANT TEST BRIEFING NOW...")
    print("--------------------------------------------------")
    job_daily_briefing() # 👈 Forcing immediate run on startup for verification!
    
    print("\nInitialization finished. Bot is sleeping until 07:30 AM Oslo time.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
