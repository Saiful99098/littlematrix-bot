# BLUE FISH PRIME Telegram Bot — লোকাল টেস্ট গাইড (Windows PC)
### Creator: LITTLE MATRIX

> **Render (live) guide:** to put this bot live on Render with the
> 5-day candle collector, database and webhook mode, see
> **README_RENDER_BN.md** — step-by-step in Bengali: GitHub push,
> Render Blueprint, environment variables, Bot Status, and testing.

> ⚠️ **প্রতিবার নতুন zip আনজিপ করার পর অবশ্যই `pip install -r requirements.txt` আবার চালাবেন।** নতুন ভার্সনে python-telegram-bot 22 লাগে (Premium বাটন আইকনের জন্য)। এটা না চালালে বাটনে ইমোজি দেখা যাবে না।

এটা লোকাল টেস্ট ভার্সন। আগে নিজের PC-তে চালিয়ে দেখবেন, সব ঠিক থাকলে
পরে ২০ দিনের ডেটা কালেক্টর যোগ করে Render-এ লাইভ করা হবে।

## এই ভার্সনে যা আছে — লাইভ মোড

- Telegram বট (polling মোড — লোকাল PC-র জন্য, webhook নয়)
- FUTURES SIGNAL: গতকালের ১ দিনের ডেটা Quotex থেকে লাইভ নিয়ে
  যেকোনো অ্যাসেটের সিগন্যাল বানায়। `.env`-এ Quotex লগইন বসানো আছে,
  `DATA_MODE=live` আছে। ১টা অ্যাসেট বাছলে ওই অ্যাসেটের, SELECT ALL
  বাছলে ৬টা অ্যাসেটের (৩টা করে ব্যাচে) ডেটা নিয়ে ক্রস-অ্যাসেট
  ফিল্টার করে ১২-১৫টার মধ্যে সিগন্যাল দেবে।
- প্রথমবার একটা অ্যাসেটের ডেটা আনলে সেটা `data/candles_<asset>_<date>.json`
  ফাইলে সেভ হয়ে যায়, দ্বিতীয়বার আর লগইন লাগে না।
- আগে থেকে ডেটা জমিয়ে রাখতে চাইলে: `python collect_yesterday.py --assets USDMXN_otc EURUSD_otc GBPUSD_otc`
- CHECKER, AI SIGNAL, MARKET LIVE, OTC LIVE, LIVE PAYOUTS, CANDLE COUNTER,
  TREND FILTER, BACKTEST, FORMATTER, SWAP C/P, TIMEZONE, TEXT FONT,
  আলাদা FILTER (Aston/Newexe), MY STATUS, ABOUT, PREMIUM (txn hash জমা)।
- ২০ দিনের কালেক্টর এই ভার্সনে নেই। `COLLECTOR_ENABLED=False` আছে,
  আর `bot/config.py`-তে TODO লেখা আছে যেখানে Render ধাপে যোগ হবে।

## যা লাগবে

1. Windows PC, Python 3.12 (python.org থেকে নামান, ইনস্টলের সময়
   "Add Python to PATH" টিক দেবেন)।
2. Telegram-এ @BotFather থেকে নেওয়া বটের token।
   (এই zip-এ `.env` ফাইলে token আগেই বসানো আছে, যাতে `python main.py`
   দিলেই বট চালু হয়। `.env` কাউকে দেবেন না, GitHub-এ পুশ করবেন না।)

## চালানোর ধাপ

### ১. Python আছে কি না চেক করুন
Start মেনুতে `cmd` লিখে Command Prompt খুলুন, লিখুন:
```
python --version
```
`Python 3.12` দেখালে ঠিক আছে।

### ২. Zip খুলুন
zip ফাইলটা যেকোনো ফোল্ডারে খুলুন (যেমন Desktop-এ `littlematrix-bot-local`)।

### ৩. Library ইনস্টল করুন — আবার চালান (জরুরি)
ওই ফোল্ডারে গিয়ে Command Prompt-এ লিখুন:
```
pip install -r requirements.txt
```
এই নতুন zip-এ Quotex-এর জন্য `beautifulsoup4`, `curl_cffi`,
`fake-useragent` যোগ করা হয়েছে। আগেরবার এইগুলো না থাকায়
লাইভ ডেটা আনতে পারছিল না, `no-data` দেখাচ্ছিল। তাই আবার
`pip install -r requirements.txt` চালাতেই হবে।
`pyquotex` আর `littlematrix` ফোল্ডার zip-এর ভেতরেই আছে,
আলাদা করে ইনস্টল করতে হবে না।

### ৪. .env — আগেই বসানো আছে
এই zip-এ `.env` ফাইল আগেই বানানো আছে, BotFather token আর Quotex
লগইন বসানো আছে, `DATA_MODE=live` করা আছে।
আলাদা করে কিছু করতে হবে না, সরাসরি ধাপ ৫-এ যান।

### ৫. বট চালান
একই Command Prompt-এ লিখুন:
```
python main.py
```
অথবা `run_bot.bat` ফাইলে ডাবল ক্লিক করুন।

"LittleMatrix bot starting (polling...)" দেখালে বট চলছে।

### ৬. Telegram-এ টেস্ট করুন
Telegram-এ আপনার বট খুলে `/start` দিন। মেনু আসবে।

টেস্ট করবেন:
- FUTURES SIGNAL → OTC FS → USDMXN_otc → 1 দিন → 09:00-10:59
  → 13টা সিগন্যাল আসা উচিত, প্রথমটা 09:02 CALL।
- CHECKER → তারিখ 2026-11-21 → লিস্ট পেস্ট করুন →
  WIN / LOSS / DOJI রেজাল্ট আসবে।
- MY STATUS → Free দেখাবে।

## এরর হলে কী জানাবেন

**Python 3.15-এ "There is no current event loop" এরর (2026-11-25-এ ঠিক করা হয়েছে):**
আপনার PC-তে Python 3.15 ছিল, তাই `python main.py` দিলে এই এরর আসতো।
নতুন zip-এ `main.py`-তে আগেই event loop বানিয়ে দেওয়া হয়েছে, তাই
এই এরর আর আসার কথা না। নতুন zip খুলে আবার `python main.py` দিন।
যদি আবারও একই এরর আসে, তাহলে Python 3.12 ইনস্টল করে ওটা দিয়ে চালান।

কোনো এরর আসলে Command Prompt-এর লেখাটা কপি করে পাঠান, আর জানান:
1. কোন ধাপে আটকেছেন (যেমন pip install, python main.py, /start)?
2. স্ক্রিনে ঠিক কী লেখা এসেছে?
3. FUTURES SIGNAL-এ কয়টা সিগন্যাল এসেছে, কী কী এসেছে?

## সতর্কতা — খুব জরুরি

- `.env` ফাইল কাউকে দেবেন না, স্ক্রিনশটে token দেখাবেন না।
- এই zip পাবলিক কোথাও আপলোড করবেন না, GitHub-এ `.env` পুশ করবেন না
  (`.gitignore`-এ `.env` বাদ দেওয়া আছে)।
- কোনো সিগন্যালই লাভের গ্যারান্টি দেবে না। এটা টেস্ট ভার্সন,
  আসল টাকায় ট্রেড করবেন না।

## টেস্ট চালানো (ঐচ্ছিক)

```
python tests/test_signal.py
```
সব ঠিক থাকলে `ALL TESTS PASSED (4/4)` দেখাবে।
