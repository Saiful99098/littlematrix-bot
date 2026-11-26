# BLUE FISH PRIME বট — Render-এ লাইভ করার গাইড

### Creator: LITTLE MATRIX

এই গাইডে ধাপে ধাপে লেখা আছে কীভাবে বটটা GitHub-এ পুশ করে Render-এ লাইভ করবেন,
আর লাইভ হওয়ার পর কীভাবে টেস্ট করবেন। লোকাল PC-র গাইডের জন্য দেখুন `README_BN.md`।

## বটটা Render-এ কী করে

- **প্রথমবার চালু হলে (backfill):** গত ৫ দিনের ডেটা সংগ্রহ করে — প্রতিদিন Asia/Dhaka
  সময় ০০:০০ থেকে ২৩:৫৯ পর্যন্ত (পুরো দিন), প্রতি অ্যাসেটে ১৪৪০টা ১ মিনিটের candle। সাথে আজকের
  ০০:০০ থেকে এখন পর্যন্ত ডেটাও নেয়, যাতে আজকের সিগন্যাল CHECKER দিয়ে মেলানো যায়।
- **তারপর প্রতি ২ ঘণ্টা পর পর:** গত ২ ঘণ্টার candle (সাথে ১০ মিনিট overlap) একই
  Quotex সেশন থেকে নিয়ে ডেটাবেসে জমায়। এক রানে একবারই লগইন হয়, ৩টা করে ব্যাচে
- **Backfill মাঝে থেমে গেলেও (সার্ভিস restart/sleep):** প্রতিটা ৩-অ্যাসেটের ব্যাচ
  আনার সাথে সাথে ডেটাবেসে বসে যায়, আর যেসব অ্যাসেটের ডেটা আগেই পুরো জমেছে
  সেগুলো পরের রানে skip হয়। তাই restart হলে আগের progress নষ্ট হয় না —
  যেখানে থেমেছিল সেখান থেকেই চলতে থাকে।
  অ্যাসেট নেয়, ব্যাচের মাঝে ১ সেকেন্ড বিরতি।
- **সিগন্যাল আর CHECKER কখনো Quotex-এ লগইন করে না** — সব ডেটাবেস থেকে আসে।
  এটাই ৪২৯ ব্লক এড়ানোর ডিজাইন।
- **ডেটাবেস:** Render-এ ফ্রি Postgres। লোকাল টেস্টে SQLite ফাইল (`data/candles.db`)।
- **Python ভার্সন:** Render Python **3.12.7** দিয়ে বিল্ড করে
  (`runtime.txt` + `.python-version` + `PYTHON_VERSION` — তিনভাবে পিন
  করা)। Python 3.14 দিয়ে বিল্ড করলে webhook চালু হয় না, দেখুন
  নিচে "ডেপ্লয় ফেইল হলে?"।
- **Bot Status বাটন:** মেনুতে নতুন বাটন — Mode, ডেটাবেসে কত candle, কোন কোন
  তারিখের ডেটা আছে, প্রতি অ্যাসেটে কত, শেষ কালেকশন কখন, পরের কালেকশন কখন,
  Quotex সেশন, আজকের কভারেজ — সব দেখায়।

---

## ধাপ ১ — GitHub-এ নতুন রিপো বানান

1. [github.com](https://github.com) খুলে লগইন করুন (ইউজারনেম: Saiful99098)।
2. উপরে ডানদিকে **+** চাপুন → **New repository**।
3. Repository name দিন: `littlematrix-bot`
4. **Public** রাখুন (Render ফ্রি প্ল্যানে public রিপো সহজ)।
5. **Add a README file** টিক দেবেন না, **.gitignore** কিছু বাছবেন না।
6. **Create repository** চাপুন।

## ধাপ ২ — PC থেকে কোড পুশ করুন

PC-তে এই প্রজেক্ট ফোল্ডারে (`littlematrix-bot-local`) Command Prompt খুলুন।
প্রথমে Git আছে কি না দেখুন:

```
git --version
```

না থাকলে [git-scm.com](https://git-scm.com/download/win) থেকে নামিয়ে ইনস্টল করুন।
তারপর একে একে এই কমান্ডগুলো চালান:

```
git init
git add .
git commit -m "Blue Fish Prime bot - Render test with 5-day collector"
git branch -M main
git remote add origin https://github.com/Saiful99098/littlematrix-bot.git
git push -u origin main
```

প্রথমবার পুশে GitHub ইউজারনেম আর password চাইবে — password-এর জায়গায়
**Personal Access Token** লাগবে (GitHub password দিয়ে পুশ হয় না)।
টোকেন বানাতে: GitHub → Settings → Developer settings → Personal access
tokens → Tokens (classic) → Generate new token → `repo` টিক দিয়ে বানান,
টোকেনটা কোথাও সেভ করে রাখুন।

> ⚠️ `.env` ফাইল কখনো GitHub-এ যাবে না — `.gitignore`-এ সেটা বাদ দেওয়া
> আছে। `git status` চালিয়ে দেখে নিন `.env` আর `session.json` লিস্টে
> নেই কি না।

পরে কোড বদলালে আবার পুশ করতে শুধু এই তিনটা কমান্ড:

```
git add .
git commit -m "যা বদলালেন সেটা লিখুন"
git push
```

## ধাপ ৩ — Render অ্যাকাউন্ট বানান

1. [render.com](https://render.com) খুলুন।
2. **Get Started** → **GitHub** দিয়ে সাইন আপ করুন (GitHub অ্যাকাউন্ট
   দিয়েই লগইন করলে রিপো কানেক্ট করা সহজ হবে)।
3. ইমেইল ভেরিফাই করুন।

## ধাপ ৪ — Blueprint দিয়ে সার্ভিস বানান

1. Render Dashboard-এ **New +** → **Blueprint** চাপুন।
2. GitHub রিপো `Saiful99098/littlematrix-bot` বাছুন (না দেখালে
   **Configure GitHub App** দিয়ে Render-কে রিপো access দিন)।
3. Render `render.yaml` ফাইলটা পড়ে দুইটা জিনিস বানাবে:
   - **Web Service:** `blue-fish-prime-bot` (ফ্রি প্ল্যান)
   - **PostgreSQL:** `littlematrix-db` (ফ্রি প্ল্যান)
4. **Apply** চাপুন। প্রথম ডেপ্লয় শুরু হবে — কয়েক মিনিট লাগবে।

> আলাদা করে Postgres বানাতে হবে না — Blueprint-ই বানিয়ে দেয়।
> `DATABASE_URL` Render নিজে বসিয়ে দেয়, আপনাকে কিছু করতে হবে না।

## ধাপ ৫ — Environment Variables বসান

ডেপ্লয় চলার সময়ই এটা করে ফেলুন:

1. Dashboard-এ **blue-fish-prime-bot** সার্ভিসটা খুলুন।
2. বাম মেনুতে **Environment** চাপুন।
3. **Add Environment Variable** দিয়ে এই তিনটা বসান (একটাও কোডে বা
   GitHub-এ লিখবেন না — শুধু এখানে):

| Key | Value |
|---|---|
| `BOT_TOKEN` | BotFather থেকে পাওয়া বটের token |
| `QUOTEX_EMAIL` | Quotex লগইন ইমেইল (`pcloss774574@gmail.com`) |
| `QUOTEX_PASSWORD` | Quotex পাসওয়ার্ড |

4. বাকিগুলো `render.yaml` থেকে নিজে নিজে বসে যাবে:
   `DATA_MODE=db`, `COLLECTOR_ENABLED=true`, `BACKFILL_DAYS=5`,
   `WEBHOOK_URL=https://<আপনার-সার্ভিস>.onrender.com`,
   `DATABASE_URL` (Postgres থেকে)।
5. **Save Changes** চাপুন — সার্ভিস আবার রিস্টার্ট হবে।

## ধাপ ৫.১ — মেম্বারদের জন্য: Channel Gate + Premium (নতুন)

মেম্বার রিলিজে আরও কয়েকটা Environment Variable লাগবে (একই Environment
পেজে বসান):

| Key | Value |
|---|---|
| `REQUIRED_CHANNEL` | চ্যানেলের সংখ্যার id, যেমন `-1001234567890` (না থাকলে gate বন্ধ থাকে) |
| `CHANNEL_INVITE_LINK` | `https://t.me/+HDiWRJothc5hNTQ9` (Join Now বাটনে এই লিংক যাবে) |
| `ADMIN_USER_IDS` | `5618029321` (আপনার Telegram user id — Premium approve করার জন্য) |
| `BINANCE_PAY_ID` | `968213178` (মেম্বাররা ৫ USDT এখানে পাঠাবে) |

আপনার Telegram user id পাবেন যেভাবে: Telegram-এ `@userinfobot` খুলে
যেকোনো মেসেজ পাঠান — ওই বট আপনার id (শুধু সংখ্যা) ফেরত দেবে। ওই
সংখ্যাটা `ADMIN_USER_IDS`-এ বসান। (আপনার id: `5618029321` — ইতিমধ্যেই
`.env.example`-এ বসানো আছে।)

চ্যানেলের id পাবেন যেভাবে:
1. Telegram-এ চ্যানেল খুলে বটটাকে **admin** বানান (Add Admins)।
2. Render Logs-এ এই লাইনটা দেখুন: `Bot added to channel '...' (id: -100...)` —
   ওই id-টাই `REQUIRED_CHANNEL`-এ বসান।
3. (বিকল্প: চ্যানেলের যেকোনো মেসেজ @userinfobot-এ forward করলেও id দেখায়।)

Premium কীভাবে চলে:
1. মেম্বার Premium বাটনে Binance Pay ID-তে ৫ USDT পাঠিয়ে transaction ID পাঠায়।
2. আপনার Telegram-এ Approve / Reject বাটনসহ মেসেজ আসে।
3. Approve চাপলে মেম্বারের ৩০ দিনের Premium চালু হয় — এটা এখন
   Postgres-এ থাকে, তাই Render রিস্টার্ট হলেও হারাবে না।

## ধাপ ৬ — লগ দেখে ডেপ্লয় ঠিক আছে কি না দেখুন

1. সার্ভিস পেজে **Logs** ট্যাব খুলুন।
2. এই লাইনগুলো দেখতে পাবেন:

```
collector: starting (backfill 5 days, then every 120 minutes)
collector: backfill starting — N assets, 5 days (...)
collector: backfill finished — N assets, M new candles, next run at HH:MM (Dhaka)
collector: backfill done — mode=db db=postgres candles=M assets=N dates=...
```

3. `Live` লেখা দেখালে সার্ভিস চালু। কোনো লাল এরর থাকলে লগের শেষ
   কয়েক লাইন কপি করে পাঠান।

## ধাপ ৭ — Telegram-এ বট খুলে Bot Status দেখুন

1. Telegram-এ বটটা খুলে `/start` দিন।
2. মেনুতে নতুন **Bot Status** বাটন চাপুন। দেখতে পাবেন:
   - Mode: Render / Database
   - Database: PostgreSQL — মোট কত candle
   - Days in DB: ৫টা তারিখের লিস্ট
   - Candles per asset: প্রতি অ্যাসেটে কত
   - Last collection / Next collection: কখন
   - Quotex session: connected / disconnected (between runs)
   - Today 00:00-now: আজকের কত candle জমেছে
3. Backfill শেষ না হলে "none yet (backfill running)" দেখাবে — কয়েক
   মিনিট অপেক্ষা করে আবার Bot Status চাপুন। Backfill শেষ হতে কয়েক
   মিনিট লাগে (অ্যাসেট যত বেশি, তত সময়)।

## ধাপ ৮ — FUTURES SIGNAL টেস্ট করুন

1. **Future Signals** → মোড বাছুন (OTC FS) → অ্যাসেট বাছুন (বা SELECT ALL)।
2. দিন বাছুন: **1 / 2 / 3 / 5 Days** (৫ দিনের ডেটা আছে, তাই সর্বোচ্চ ৫)।
3. সময় বাছুন: **09:00-10:59** বা **Custom Time**।
4. রেজাল্টে দেখবেন: `Dates: 2026-11-20 to 2026-11-24 | Days: 5 (database)`।
5. ডেটা কম থাকলে লেখা থাকবে: "You picked 5 days, but the database
   has only 3 day(s) so far — using 3 day(s)"।

## ধাপ ৯ — CHECKER দিয়ে আজকের সিগন্যাল মেলান

1. **Check Signals** চাপুন — Date: আজকের তারিখ দেখাবে।
2. আজকের সিগন্যাল লিস্ট পেস্ট করুন, যেমন:
   ```
   USDMXN-OTC,09:02 M1 CALL
   USDMXN-OTC,09:11 M1 CALL
   ```
3. রেজাল্টে শুধু Premium সিম্বল দেখাবে (✅ ❌ ➖ ❓) আর নিচে
   Total আর Accuracy। আজকের candle ডেটাবেসে আছে (backfill + ২ ঘণ্টা
   পর পর কালেকশন), তাই আজকের সিগন্যাল মেলানো যাবে।

## ধাপ ১০ — ৪২৯ এলে যা করবেন

লগে বা বটে এই মেসেজ দেখলে:

> Quotex temporarily blocked us (429). Please wait 15-30 minutes, do not retry.

1. **কিছু চাপবেন না** — বাটন বারবার চাপলে, বট রিস্টার্ট করলে ব্লক
   আরো বাড়ে।
2. কালেক্টর নিজে ১০ মিনিট পর আবার চেষ্টা করবে। লগে দেখবেন।
3. ১৫-৩০ মিনিট অপেক্ষা করুন, তারপর প্রথমে **একটা** অ্যাসেট দিয়ে
   টেস্ট করুন — SELECT ALL দিয়ে নয়।
4. ডেটাবেসে আগের ডেটা থাকায় সিগন্যাল আর CHECKER তখনো চলবে —
   শুধু নতুন কালেকশন থেমে থাকবে।

---

## ডেপ্লয় ফেইল হলে?

প্রথম ডেপ্লয়ে (২০২৬-১১-২৫) এই এরর এসেছিল:

```
telegram.error.NetworkError: Unknown error in HTTP implementation:
NoEventLoopError('Not currently running on any asynchronous event loop.')
```

কারণ: Render `runtime.txt`-এ লেখা Python 3.12.7-কে না মেনে Python 3.14
দিয়ে বিল্ড করেছিল। Python 3.14-এ `asyncio.get_event_loop()` আর নিজে
লুপ বানায় না, তাই webhook চালু করার সময় (setWebhook) লুপ না পেয়ে
বটটা ক্র্যাশ করেছিল।

ঠিক করা হয়েছে:
- `.python-version` ফাইলে `3.12.7` লেখা আছে, আর `render.yaml`-এ
  `PYTHON_VERSION=3.12.7` বসানো আছে — Render এখন 3.12 দিয়েই বিল্ড করবে।
- `main.py` এখন বট বানানোর আগেই event loop তৈরি করে, তাই 3.14/3.15-তেও
  webhook চালু হবে।

আবার ফেইল হলে: কোডের নতুন ভার্সন push করুন (Render নিজে আবার ডেপ্লয়
করবে), তারপর Logs-এ দেখুন — `Application started` লেখা আসলেই বট চালু।
লাল এরর থাকলে লগের শেষ কয়েক লাইন কপি করে পাঠান।

---

## Quotex 403 এলে (Render থেকে লগইন হয় না)?

বট Live হওয়ার পরও লগে `collector: run failed: Quotex login failed:
Error on access page: 403` দেখলে বুঝবেন Quotex Render-এর সার্ভারের
IP থেকে লগইন নিচ্ছে না। তখন **PC থেকে কালেক্টর চালান** — পুরো গাইড
দেখুন `README_PC_COLLECTOR_BN.md` (PC থেকে সরাসরি Render-এর
Postgres ডেটাবেসে candle লেখে)।

**Render থেকেই ঠিক করার আরও দুইটা উপায়:**

1. **Region বদলান (Singapore):** 403 আসলে শুধু ডেটা সেন্টারের
   IP-র দোষ নয় — Quotex যুক্তরাষ্ট্র, কানাডা, হংকং, রাশিয়া আর
   ইউরোপের সব দেশ (EEA) থেকে ক্লায়েন্ট নেয় না, ওখানে "not
   available in your region" দেখায়। Render-এর ডিফল্ট region
   Oregon, USA — তাই Render-এর সার্ভার থেকে Quotex 403 দেয়।
   Dashboard → `blue-fish-prime-bot` সার্ভিস → **Settings** →
   **Region** → **Singapore** বাছুন। সিঙ্গাপুর Quotex-এর নিষিদ্ধ
   তালিকায় নেই, তাই ওখান থেকে লগইন হওয়ার কথা। ⚠️ Frankfurt
   (জার্মানি, EEA) বা Oregon (USA) বাছবেন না — ওই দুই জায়গা
   থেকেও 403 আসবে। Region বদলালে সার্ভিস আবার তৈরি হবে, আর
   Environment Variables আবার দেখে নিন — BOT_TOKEN,
   QUOTEX_EMAIL, QUOTEX_PASSWORD ঠিক আছে কিনা।

2. **Proxy ব্যবহার করুন:** residential/mobile proxy থাকলে
   Render Environment-এ `QUOTEX_PROXY` বসান
   (যেমন `http://user:pass@host:port`), তারপর redeploy করুন।
   কালেক্টর তখন proxy দিয়ে Quotex-এ লগইন করবে। ফ্রি public
   proxy ব্যবহার করবেন না — ওগুলো অবিশ্বস্ত আর প্রায়ই ব্লক
   থাকে। আর proxy-র পাসওয়ার্ড কখনো চ্যাটে পাঠাবেন না।

> `QUOTEX_HOST` নামে আরও একটা সেটিং আছে (অন্য broker host
> চেষ্টা করার জন্য), কিন্তু কোনো বিকল্প host 403 এড়ায় বলে
> যাচাই করা হয়নি — proxy আর region না হলে তবেই ওটা চেষ্টা
> করবেন।

---

## জেনে রাখা ভালো

- **ফ্রি প্ল্যান ঘুমায়:** ১৫ মিনিট কোনো ট্রাফিক না থাকলে Render ফ্রি
  সার্ভিস ঘুমিয়ে যায়। Telegram মেসেজ এলে জেগে ওঠে, কিন্তু ঘুমন্ত
  অবস্থায় ২ ঘণ্টা পর পর কালেকশন চলবে না। ২৪/৭ কালেকশন চাইলে
  Dashboard → Settings → **Instance Type: Starter** (মাসে প্রায় $৭)
  করুন।
- **কোড বদলালে:** PC-তে বদলে `git push` করুন — Render নিজে নতুন
  ভার্সন ডেপ্লয় করবে, বট ১-২ মিনিটের জন্য রিস্টার্ট হবে।
  `.env`, token, পাসওয়ার্ড কখনো GitHub-এ পুশ করবেন না — ওগুলো শুধু
  Render Environment Variables-এ থাকে।
- **ইউজার/প্রিমিয়াম ডেটা:** এই ভার্সনে ইউজার আর প্রিমিয়াম অনুরোধ
  SQLite ফাইলে থাকে — Render-এ সার্ভিস রিস্টার্ট হলে সেটা মুছে যায়।
  এটা পরের ধাপে Postgres-এ নেওয়া হবে। Candle ডেটা Postgres-এ
  থাকে, তাই সেটা নিরাপদ।
- **CHECKER-এ MTG1 বাছাই আর তারিখ:** Check Signals চাপলে বট প্রথমে জিজ্ঞেস করবে MTG1 রেজাল্ট
  চান নাকি শুধু M1 (Non-MTG), তারপর কোন তারিখের রেজাল্ট চান — তারিখটা লিখে পাঠান
  (যেমন 2026-11-25) বা Today চাপুন। বট সেই তারিখের candle দিয়ে চেক করবে। MTG1 বাছলে
  হারা সিগন্যাল MTG1-এ জিতলে লাইনে WIN স্টিকারের পরে একটা ডট থাকবে (✅.) আর হারলে শুধু
  LOSS স্টিকার থাকবে — পাশে (MTG1: ...) লেখা থাকবে না। নিচে MTG1 Accuracy আলাদা করে
  লেখা থাকবে। রেজাল্ট লাইনে তীর (→) নেই — শুধু WIN/LOSS-এর স্টিকার।
- **Signal Formatter এখন ফরম্যাট উল্টায়:** `USDPHP-OTC,23:45 M1 PUT` দিলে
  `USDPHP_otc,23:45 M1 PUT` ফেরত দেবে, আর `BTCUSD_otc,10:57 M1 PUT` দিলে
  `BTCUSD-OTC,10:57 M1 PUT` ফেরত দেবে।
- **লগে যা দেখবেন:** প্রতি ২ ঘণ্টার রানে `collector: 2-hourly run
  finished — N assets, M new candles` লেখা আসে।
