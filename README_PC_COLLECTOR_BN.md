# PC থেকে কালেক্টর চালানো — Render 403-এর সমাধান

### BLUE FISH PRIME বট — Creator: LITTLE MATRIX

## কেন এটা দরকার

Render-এ বট Live (webhook 200 OK), কিন্তু Render-এর সার্ভার থেকে
Quotex লগইন করলে Quotex 403 দিয়ে ফিরিয়ে দেয় (`Error on access
page: 403`)। কারণ Render-এর ডিফল্ট region Oregon, USA — আর
Quotex যুক্তরাষ্ট্র, কানাডা, হংকং, রাশিয়া আর ইউরোপের সব দেশ
(EEA) থেকে ক্লায়েন্ট নেয় না, ওখানে "not available in your region"
দেখায়। তাই Render-এর কালেক্টর কোনো candle জমাতে পারে না,
Bot Status-এ 0 candle দেখায়।

তোমার বাড়ির PC-র IP থেকে একই Quotex লগইন হয়। তাই এই স্ক্রিপ্ট
(`collect_to_db.py`) PC-তে চালালে ও সরাসরি Render-এর Postgres
ডেটাবেসে candle লিখে দেয়। বট Render-এই থাকে, শুধু ডেটা PC থেকে
আসে। বটের কোনো সেটিং বদলাতে হয় না।

---

## ধাপ ১ — Render থেকে External Database URL কপি করুন

1. [render.com](https://render.com) খুলে লগইন করুন।
2. Dashboard-এ **`littlematrix-db`** ডেটাবেসটা খুলুন।
3. উপরে **Info** ট্যাব চাপুন।
4. নিচে **External Database URL** লেখা একটা লম্বা লিংক পাবেন —
   ওটার পাশে **Copy** চাপুন।

> ⚠️ **External** Database URL-টাই নেবেন, **Internal**-টা নয়।
> Internal URL শুধু Render-এর ভেতর থেকে কাজ করে, PC থেকে কাজ
> করবে না।

## ধাপ ২ — PC-র `.env` ফাইলে DATABASE_URL বসান

প্রজেক্ট ফোল্ডারে (যেখানে `main.py` আছে) `.env` ফাইলটা খুলুন।
ওখানে এই লাইনগুলো থাকতে হবে — শুধু key-র নাম দেখে নিন, মানগুলো
আপনার নিজের:

```
BOT_TOKEN=<আপনার বটের টোকেন — আগেই আছে>
QUOTEX_EMAIL=<আপনার Quotex ইমেইল — আগেই আছে>
QUOTEX_PASSWORD=<আপনার Quotex পাসওয়ার্ড — আগেই আছে>
DATABASE_URL=<ধাপ ১ থেকে কপি করা External Database URL>
```

`DATABASE_URL=`-এর পর ধাপ ১-এ কপি করা পুরো লিংকটা বসাবেন।
লিংকটা কাউকে পাঠাবেন না, GitHub-এ পুশ করবেন না — `.env`
ফাইল কখনো GitHub-এ যায় না।

## ধাপ ৩ — স্ক্রিপ্ট চালান

প্রজেক্ট ফোল্ডারে Command Prompt (বা Git Bash) খুলে প্রথমে একবার
এটা চালান (psycopg2 ড্রাইভার লাগবে):

```
pip install -r requirements.txt
```

তারপর কালেক্টর চালান:

```
python collect_to_db.py
```

এটা প্রথমে গত ৫ দিনের ডেটা (প্রতিদিন ঢাকা সময় ০০:০০–১১:৫৯)
+ আজকের ০০:০০ থেকে এখন পর্যন্ত নেবে, তারপর প্রতি ২ ঘণ্টা পর
পর নতুন candle নিয়ে ডেটাবেসে জমাবে। লগে দেখবেন:

```
pc-collector: backfill finished — N assets, M new candles, next run at HH:MM (Dhaka)
```

শুধু একবার চালিয়ে থামাতে চাইলে:

```
python collect_to_db.py --once
```

এটা একবার backfill + একবার ২ ঘণ্টার রান করে বন্ধ হয়ে যাবে।

> Telegram বট চালানোর দরকার নেই — এই স্ক্রিপ্ট একাই কাজ করে।

## ধাপ ৪ — যাচাই করুন

1. Telegram-এ বট খুলে **Bot Status** বাটন চাপুন।
2. দেখবেন **Database: PostgreSQL — N candles**, **Days in DB**-তে
   তারিখের লিস্ট, **Today 00:00-now**-এ সংখ্যা বাড়ছে।
3. Render-এর বটের Logs দেখার দরকার নেই — ডেটা PC থেকে যাচ্ছে।

## ধাপ ৫ — চালু রাখা

- **সহজ উপায়:** Command Prompt-এর উইন্ডোটা খোলা রাখুন। উইন্ডো
  বন্ধ করলে কালেকশন থেমে যাবে (বট চলতে থাকবে, পুরনো ডেটা
  দিয়েই সিগন্যাল দেবে)।
- **অটো উপায় (Task Scheduler):** PC রিস্টার্ট হলেও যাতে নিজে
  চালু হয়:
  1. Windows Search-এ **Task Scheduler** লিখে খুলুন।
  2. ডানদিকে **Create Basic Task...** চাপুন।
  3. Name দিন: `Blue Fish Prime Collector`, Next।
  4. Trigger: **Daily**, Next। Start-এ আজকের তারিখ রাখুন।
  5. **Action:** Start a Program, Next।
  6. Program/script-এ লিখুন: `python`
     Add arguments-এ লিখুন: `collect_to_db.py --once`
     Start in-এ লিখুন: প্রজেক্ট ফোল্ডারের পুরো পথ
     (যেমন `C:\Users\knull\Desktop\BLUE FISH PRIME\littlematrix-bot-local (14)\littlematrix-bot-local`
     — আপনার ফোল্ডারের নাম অনুযায়ী বদলে নিন)।
  7. Finish-এর আগে **"Open the Properties dialog"** টিক দিন,
     Properties-এ **Triggers** ট্যাবে গিয়ে **Edit** চাপুন,
     **"Repeat task every: 2 hours"** বাছুন, **"for a duration
     of: Indefinitely"** বাছুন, OK চাপুন।
  8. এরপর প্রতি ২ ঘণ্টা পর পর Task Scheduler নিজে স্ক্রিপ্টটা
     চালাবে (`--once` মানে একবার করে চালিয়ে থামবে)।

## PC থেকেও 403 এলে কী করবেন

লগে `Quotex blocked this PC too (403 on access page)` লেখা দেখলে:

1. প্রথমে ব্রাউজারে [quotex.com](https://quotex.com) খুলে একই
   ইমেইল-পাসওয়ার্ড দিয়ে লগইন হয় কিনা দেখুন।
2. **ব্রাউজারেও লগইন না হলে:** অ্যাকাউন্টের সমস্যা — পাসওয়ার্ড
   ভুল, বা অ্যাকাউন্টে সমস্যা হয়েছে। পাসওয়ার্ড ঠিক করে `.env`
   আপডেট করুন।
3. **ব্রাউজারে লগইন হলে কিন্তু স্ক্রিপ্টে 403 এলে:** Quotex এই
   IP থেকে API লগইন আটকাচ্ছে। কয়েক ঘণ্টা অপেক্ষা করে আবার
   চালান, বারবার চেষ্টা করবেন না (বেশি চেষ্টা করলে ব্লক বাড়ে)।
   Proxy থাকলে `.env`-এ `QUOTEX_PROXY=http://user:pass@host:port`
   বসিয়ে চালাতে পারেন (কালেক্টর তখন proxy দিয়ে লগইন করবে)।

## জেনে রাখা ভালো

- Render-এর কালেক্টর 403 খেয়ে যাচ্ছে, তাই ওটা থেকে কোনো সমস্যা
  নেই — PC-র কালেক্টরই ডেটা জমাবে। একই candle দুই জায়গা থেকে
  এলেও ডুপ্লিকেট হয় না (ডেটাবেসে UNIQUE নিয়ম আছে)।
- PC বন্ধ থাকলে বা ইন্টারনেট না থাকলে নতুন ডেটা জমবে না —
  PC চালু করে স্ক্রিপ্ট আবার চালালেই বাকি ডেটা জমে যাবে।
- `429` এলে স্ক্রিপ্ট নিজে ১০ মিনিট অপেক্ষা করে একবার আবার
  চেষ্টা করে, তারপর পরের সাইকেলের জন্য অপেক্ষা করে।
