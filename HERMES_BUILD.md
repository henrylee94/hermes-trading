# HERMES — Complete Build Instructions (single source of truth)
**You are the Hermes deployment agent. This file + `hermes_dashboard_final.html` are everything you need. Read this file fully, then build it. Assume you have no other context. Build EXACTLY what is written. When anything conflicts, THIS document wins.**

Owner: Henry · REDACTED · Timezone **MYT (UTC+8)**.

---

## HOW TO USE THIS DOC
1. Read all sections.
2. Build in this order: **Module 0 (shared data) → Pipeline A (长投) → Pipeline C (大佬) → Pipeline B (做T) → web server + Telegram bot.**
3. The website is already built: use `hermes_dashboard_final.html` as the template. Your job is to feed it REAL data in the exact JSON shapes in §DATA, and run the server/bot/cron.
4. Self-check against §ACCEPTANCE after each pipeline.

---

## §RULES — GLOBAL (non-negotiable)
1. **LIVE DATA ONLY. NEVER fabricate** any price, filing, tweet, CIK, ticker, or number. If a value can't be fetched → output `"数据缺失"` / `null` and exclude it from any calculation that needs it. The `LONG`/`SMART`/`SWING` constants inside the HTML are a VISUAL MOCK — replace them entirely with live data. Never show mock data as if real.
2. **PLAIN-LANGUAGE beginner Chinese output.** The user has no finance background. Translate every jargon term before it reaches the screen/Telegram:
   - RS-Ratio number → words: ≥103「明显比大盘强」,101–103「比大盘强」,99–101「和大盘差不多」,97–99「比大盘弱」,<97「明显比大盘弱」.
   - Altman Z → 「破产风险 低」(Z≥2.6) / 「中」(1.1–2.6) / 「高」(<1.1).
   - Piotroski F-Score → 「财务健康 X/9」. Value-trap flag count → 「危险信号 X/7」.
   - Event-study residual / z-score → never shown; say 🟢「只是跟着大盘/板块回调,公司没事」or 🔴「它自己单独大跌,因为<原因>」.
3. **MINIMIZE LLM (save tokens).** ALL math/screening/levels/diffs are deterministic CODE. LLM is used ONLY for narrative + cause-classification + opinion summary, **batched to ≤2 calls per pipeline run** (never per-stock/per-account loops). Pipeline B (做T) and the website and the JSON/Telegram formatters make **ZERO** LLM calls. Feed the LLM only pre-computed numbers; it must not compute.
4. **NOT investment advice. NO auto-trading, ever.** Recommend prices only; Henry places all orders. Every web tab and every Telegram message ends with `⚠ 仅供参考,非投资建议`.
5. **FAIL-SAFE.** When unsure, pick the safe side: unknown earnings date → PAUSE 做T on that name; missing data → exclude; trend uncertain → no grid.

---

## §TIMEZONE & MARKET CALENDAR (many behaviors depend on this)
- Henry = **MYT (UTC+8)**. Markets = NYSE/Nasdaq **US/Eastern**. US regular session 09:30–16:00 ET ≈ **21:30–04:00 MYT** (shifts with US DST — use a TZ library, never hardcode).
- **Rule:** all "trading day / yesterday / this week / pivots / earnings-in-N-days" logic computes in **US/Eastern** using an **NYSE calendar** (`pandas_market_calendars`). "Last completed session" = most recent closed NYSE session (handles weekends & US holidays). All **cron times are MYT**.
- Cron (MYT):
  | Job | cron (MYT) | meaning |
  |---|---|---|
  | A 长投 weekly | `0 9 * * 6` | Sat 09:00, after Fri US close |
  | C 大佬 daily | `0 9 * * 2-6` | 09:00 Tue–Sat, after each US session |
  | B 做T pool weekly | `0 8 * * 6` | Sat 08:00, refresh ~20 pool |
  | B 做T levels daily | `0 17 * * 1-5` | 17:00 Mon–Fri, BEFORE the upcoming US open; uses last completed US session + pre-market |
  | B Telegram bot | always-on service | on-demand |

---

## §SECRETS (store in `.env`, gitignored; never write into files served on localhost)
| Secret | Used by | Required |
|---|---|---|
| FINNHUB_API_KEY | A, C | yes |
| TELEGRAM_BOT_TOKEN | push + bot | yes |
| TELEGRAM_CHAT_ID | restrict bot to Henry | yes |
| LLM_API_KEY | A & C narrative | yes |
| OPENFIGI_KEY | C CUSIP→ticker | recommended |
| REDDIT_CLIENT_ID / REDDIT_SECRET | C crowd | optional |
| X_API_KEY | C influencers | optional |

`requirements.txt`: `yfinance>=0.2.62, finnhub-python, pandas, numpy, pyarrow, lxml, beautifulsoup4, curl_cffi, requests, statsmodels, pandas_market_calendars, python-telegram-bot, python-dotenv, fastapi, uvicorn` + your LLM client.

---

## §DISK LAYOUT
```
hermes/
  .env  config.json  tracked_entities.json  swing_watchlist.json
  cache/  (universe_latest.json, prices_latest.parquet, cusip_ticker.json, 13f_<acc>.json, ark_<fund>_prev.csv, x_userids.json)
  logs/   (hermes_YYYYMMDD.log)
  journal/recommendations.csv
  hermes_site/
     index.html         (= hermes_dashboard_final.html, wired to fetch the JSON below)
     data/value/index.json   data/value/2026-W26.json ...
     data/smart/index.json   data/smart/2026-06-27.json ...
     data/swing/index.json   data/swing/2026-06-29.json ...
```

---

## §CONFIG FILES (canonical schemas — the contract between site and pipelines)
### config.json (numeric knobs; written by the website `/api/config`, read by pipelines)
```jsonc
{ "long":  {"roe":12,"roic":10,"de":1.0,"cur":1.5,"mktcap":2,"fscore":7,"maxpe":15,"mos":25},
  "swing": {"equity":50000,"budget":10000,"maxpos":5,"risk":1,"target":200,"stepk":0.5,
            "stopmult":1.5,"commission":0,"atrmin":2.5,"atrmax":6,"adxmax":20,"pool":20,"dailystop":500} }
```
Read paths: `config["long"]["roe"]`, `config["swing"]["budget"]`, etc. Units: roe/roic/mos = %, mktcap = $B, risk = %. Map at read (`MIN_ROE = long.roe/100`). `/api/config` POST must VALIDATE ranges before writing (roe/roic 0–60, de 0–5, cur 0–10, mktcap 0–5000, fscore 0–9, maxpe 1–100, mos 0–80, equity>0, budget>0, risk 0.1–5, target 10–5000, stepk 0.1–3, stopmult 0.5–5, atr 0.5–20, adxmax 5–40, pool 3–60); reject+keep old on invalid.

### tracked_entities.json (Pipeline C — authoritative list of who to track)
```jsonc
{ "funds_13f":[{"name":"Berkshire Hathaway Inc","cik":"0001067983"},
               {"name":"Pershing Square Capital Management","cik":null},
               {"name":"Greenlight Capital","cik":null},
               {"name":"Duquesne Family Office","cik":null},
               {"name":"ARK Investment Management LLC","cik":null}],
  "ark_funds":["ARKK","ARKW","ARKG","ARKQ","ARKF"],
  "reddit_subs":["wallstreetbets","stocks","investing","ValueInvesting"],
  "x_accounts":["michaeljburry","BillAckman","DeItaone","unusual_whales","KobeissiLetter","charliebilello","LizThomasStrat","aleabitoreddit"] }
```
`cik:null` → resolve at runtime (§C). Never trust a memorized CIK except Berkshire (stable).

### swing_watchlist.json (Pipeline B — the ONLY home of the 做T pool)
```jsonc
{ "auto":["PLTR","AMD","MU","SOFI","BAC", ...],   // weekly screen rewrites ONLY this
  "pins":[{"sym":"PLTR","fixed_shares":100},{"sym":"SNDK","fixed_shares":5}] }  // manual; NEVER clobbered; edited via Telegram /add /remove
```
Effective pool = unique(auto ∪ pins).

Display prefs (show counts, sort, panel/source toggles) live in browser localStorage only — pipelines ignore them.

---

## §DATA — output JSON the website reads (match field-for-field; the HTML renderers expect exactly these)
Three cadences → three folders, each with an index (newest first). Each pipeline writes its own folder and prepends to its index. These writers make ZERO LLM calls.

**value/{ISOweek}.json** (Pipeline A, weekly):
```jsonc
{ "generated_at":"2026-06-27 09:02","range":"2026-06-22 ~ 2026-06-27",
  "digest":{"sentiment":"pos|neu|neg","score":50,"text":"人话市场总结"},   // score 0-100 drives the meter
  "market_stats":[{"k":"标普周涨跌","v":"+0.4%"}, ...],
  "rrg":[{"s":"科技 XLK","x":103.5,"y":101.8,"q":"Leading|Improving|Weakening|Lagging"}, ...11 sectors],
  "leaders":"AI硬件 VRT · ANET  ·  光学 COHR",          // pre-formatted plain string
  "zone":[{"sym":"STX","name":"Seagate(希捷·硬盘)","tag":"g|r","price":92.4,"buy":95.0,"low":78,"high":118,
           "momentum_ok":true,"reason":"人话原因","flags":"危险信号 0/7 · 财务健康 8/9 · 破产风险 低","news":[{"t":"标题","u":"url"}]}],
  "watch":[{"sym":"GOOGL","name":"Alphabet","price":182,"buy":165,"gap":"10.3%","pe":21,"roe":"29%"}],
  "funnel":[{"n":553,"l":"universe"},{"n":128,"l":"通过质量"},{"n":41,"l":"低估"},{"n":14,"l":"观察"},{"n":5,"l":"入场区"}] }
```
**smart/{date}.json** (Pipeline C, daily):
```jsonc
{ "as_of":"2026-06-27 20:18",
  "crossref":[{"ticker":"GOOGL","text":"你观察名单里的 <b>GOOGL</b>:今日 3 位高管集体买入 + ARK 加仓"}],
  "fund_moves":[{"ticker":"AAPL","name":"Apple","action":"加仓","fund":"Berkshire 巴菲特","trust":"official"}],
  "insider_buys":[{"ticker":"GOOGL","name":"Alphabet","insiders":3,"usd":"240万","cluster":true,"trust":"official"}],
  "ark":[{"ticker":"COHR","name":"Coherent 光学","action":"买入","trust":"official"}],
  "crowd":[{"ticker":"NVDA","mentions":820,"stance":"看涨","trust":"crowd"}],
  "influencers":[{"handle":"aleabitoreddit","name":"Serenity","summary":"看好AI算力","tickers":["CRWV"],"stance":"看涨","trust":"unverified","note":"自报战绩,无法核实"}],
  "sources_ok":{"edgar":true,"openfigi":true,"ark":true,"reddit":true,"x":false} }
```
`trust` ∈ official(🟢) | crowd(🟡) | unverified(🔴). If X disabled, omit influencers / set sources_ok.x=false.

**swing/{date}.json** (Pipeline B, daily):
```jsonc
{ "as_of":"2026-06-29 08:32","pool":20,"sources_ok":{"price":true,"intraday":true},
  "stocks":[
    {"sym":"PLTR","name":"Palantir 帕兰提尔","price":98.4,"regime":"range","box_low":94,"vwap":98.9,"box_high":106,
     "stop":92,"step":2,"shares":83,"profit_per":"约 +$166","rsi":"超卖","adx":"区间震荡","earn_days":34,
     "buy_levels":[{"price":98,"shares":83},{"price":96,"shares":83}],
     "sell_levels":[{"price":104,"shares":83},{"price":102,"shares":83},{"price":100,"shares":83}],
     "action_now":"现价 $98.4 接近买入区 $98,RSI 超卖,可挂 83 股 @ $98,目标卖 $100(约 +$166)。"},
    {"sym":"NVDA","name":"英伟达","price":178,"regime":"trend","price_only":true,"pause_reason":"上涨趋势中,暂停做T。"}
  ] }
```
`rsi`/`adx` are PLAIN strings. Paused stock = only `{sym,name,price,regime:"trend",price_only:true,pause_reason}`.

### Wire the HTML to live data
In `hermes_dashboard_final.html`, replace the three mock `const LONG/SMART/SWING` with fetches:
```js
const LONG  = await (await fetch('data/value/'+latestWeek+'.json')).json();   // build a small loader keyed off each index.json
// or load all listed in index, keyed by id, exactly like the current objects are keyed.
```
Keep `render()/renderSmart()/renderSwing()` and the pickers as-is — only the data source changes. Acceptance: no `LONG/SMART/SWING` literal data survives.

---

## §LLM USAGE (exact, batched)
- Pipeline A: **1 call** → input = {all IN_ZONE tickers + their pre-computed news headlines & fundamental deltas, sector performance table, market stats}; output JSON = {per-ticker 🟢/🔴 + one-line reason, digest text, digest score, sector narrative}. Temperature 0.2. Verify every number it echoes exists in the input (drop fabricated).
- Pipeline C: **1 call** → input = {each tracked X account's recent tweets, top Reddit tickers + headlines}; output JSON = {per-account summary+stance+tickers (tagged unverified), per-ticker crowd stance}.
- Pipeline B: **0 calls.** `action_now` is a code template.
- Website / Telegram formatter / JSON writers: **0 calls.**

---

## §A — PIPELINE A: 长投 (weekly value investing)
**Module 0 (shared, run first):**
- Universe = S&P500 + Nasdaq100. Build from Wikipedia (`User-Agent` header required; S&P table[0] col "Symbol"; Nasdaq pick table by columns "Ticker"+"Company"; dedupe dual-class; canonical sector = Wikipedia **GICS Sector**). yfinance symbols use dash (BRK-B), finnhub uses dot (BRK.B) — keep both. Cache → `cache/universe_latest.json`.
- Prices: ONE batched `yf.download(yf_syms, period="1y", interval="1d", group_by="ticker", threads=2, session=curl_cffi.Session(impersonate="chrome"))`. Compute price, ma50, ma200, 52w high/low, ret_252, ret_21. Cache → `prices_latest.parquet`. (curl_cffi session is the key fix for yahoo rate-limits.)
- Fundamentals: finnhub `client.company_basic_financials(sym,"all")["metric"]`, throttle 1.1s/call (≤60/min), retry on 429. Keys: `peTTM,pbAnnual,roeTTM,roiTTM,"totalDebt/totalEquityAnnual"(normalize /100),currentRatioAnnual,freeCashFlowTTM,epsTTM,bookValuePerShareAnnual,marketCapitalization,enterpriseValue,beta,grossMarginTTM,revenueGrowth5Y,dividendsPerShareTTM,payoutRatioTTM`, plus `series.annual` for trend tests. Use `.get`, treat missing as None (never 0). PE is None when EPS<0 — handle.
- **Exclude Financials & Utilities sectors and ADRs from the value screen** (Altman/Magic-Formula invalid for them). They may still appear in sector rotation via ETFs. Zone/watch must NEVER contain excluded-sector tickers.

**A1 quality (hard gates, ALL required):** roe≥0.12, roic≥0.10, debt/equity<1.0, current_ratio≥1.5, fcf_ttm>0, market_cap>2e9, eps_ttm>0.
- **Altman Z (non-mfg):** `Z=6.56*X1+3.26*X2+6.72*X3+1.05*X4`; X1=(CA−CL)/TA, X2=RE/TA, X3=EBIT/TA, X4=bookEquity/TL. Reject Z<1.1. (Pull line items from finnhub series.annual or yfinance balance sheet; if unavailable record null, don't reject on missing.)
- **Piotroski F-Score (need ≥7):** 9×1pt: ROA>0; CFO>0; ΔROA>0; CFO/TA>ROA; LT-debt ratio↓; current ratio↑; no new shares; gross margin↑; asset turnover↑.

**A2 value (cheapness):** earnings_yield=EBIT/EV; roc=EBIT/(net_fixed_assets+net_working_capital); MagicFormula rank = rank(earnings_yield)+rank(roc) (lower=better). Keep names with pe ≤ sector_median_pe AND pe ≤ 15; pb ≤ 1.5 (or pe*pb ≤ 22.5); prefer peg<1 (peg=pe/(rev/eps growth%)). Sort survivors by MagicFormula score.

**A3 fair value (use NORMALIZED eps = mean last 3y):** graham=√(22.5*eps_norm*bvps) (skip if bvps≤0); fv_pe=eps_norm*min(company_5y_median_pe, sector_median_pe, 15); **fair_value=min(graham, fv_pe)**.

**A4 entry:** recommended_buy=fair_value*(1−mos) (mos default 0.25); gap_pct=(price−buy)/buy; status=IN_ZONE if price≤buy else WATCH. momentum_ok = price>ma200 OR ret_21>0 (flag only, don't reject).

**A5 drop diagnoser (only for IN_ZONE; batched 1 LLM call):**
- Quant trap flags (count of 7): rev_growth_5y<0 or rev_ttm<0; gross_margin lower than 3y ago; debt/equity rising; fcf<0 or falling; F≤3; dividend cut; Altman Z<1.81.
- Event-study residual: fit `R_stock=α+β·R_SPY` on ~250 trading days ending 6 days BEFORE the drop window (gap avoids contamination); AR=actual−(α+β·R_SPY_event); z=AR/resid_std. |z|<1.96 & moved with SPY → likely 🟢 sentiment; z<−1.96 → idiosyncratic → company news.
- Final tag: 🔴 if (LLM says fundamental) OR trap_flags≥2; else 🟢. `flags` shown in 人话.

**A6 sector rotation (RRG):** 11 SPDR ETFs `XLK XLF XLE XLV XLY XLP XLI XLB XLU XLRE XLC` + SPY. RS=(sector/SPY)*100; RS_Ratio=100+sma((RS−sma(RS,n))/std(RS,n),4) with n≈55 daily; RS_Mom=100+sma(roc(RS_Ratio)…). Quadrant: Ratio>100&Mom>100=Leading(领涨); <100&>100=Improving(轮入=下一个); >100&<100=Weakening(转弱); else Lagging(回避). Use `.shift(1)` (no look-ahead). Leaders within hot sectors = top by 12-1 momentum (price_{t-21}/price_{t-252}-1).

**A7 digest (part of the 1 LLM call):** plain narrative from sector perf + market news; temperature 0.2; cite only supplied data; output also `digest.score` (deterministic: blend SPY weekly return, VIX level, % above 200DMA into 0–100 — compute in code, LLM only narrates).

Output → `value/{week}.json` (§DATA) + Telegram weekly summary + site link.

---

## §B — PIPELINE B: 做T (swing/grid). THREE cadence layers.
**Layer 1 weekly pool:** scan S&P500+Nasdaq100, pick ~`pool` (20) best: liquidity (Close>5, SMA20(vol)>1M, $vol>20M), ATR% in [atrmin,atrmax] (=ATR(14)/Close*100), beta 0.8–1.8, **range-bound ADX(14)<adxmax(20)**, price>SMA200, no earnings within 7d. Write `swing_watchlist.json.auto` (don't touch pins).
**Layer 2 daily evening (17:00 MYT):** for the effective pool, recompute levels from last completed US session + pre-market; rank best to 做T; write `swing/{date}.json`; Telegram push.
**Layer 3 on-demand:** Telegram bot recomputes live on `/dot` (see §TELEGRAM).

**Indicators (all code):**
- ATR(14) Wilder: TR=max(H−L,|H−Cprev|,|L−Cprev|); ATR=ewm(alpha=1/14). expected_daily=ATR; expected_weekly=ATR*√5.
- Pivots from last session H/L/C: P=(H+L+C)/3; R1=2P−L,S1=2P−H; R2=P+(H−L),S2=P−(H−L). Camarilla S3=C−1.1*(H−L)/4, R3=C+1.1*(H−L)/4 (buy near S3/sell R3), S4/R4 break=breakout.
- Bollinger(20,2): Mid=SMA20; Upper/Lower=Mid±2*std; %b=(C−Lower)/(Upper−Lower).
- VWAP (intraday bars, resets daily) = Σ(typical*vol)/Σvol, typical=(H+L+C)/3.
- Donchian(20): BoxHigh/Low = 20d high/low. RSI(2) (Connors), Stochastic(14,3,3).

**Grid:** step=stepk×ATR (default 0.5; matches Henry's $1-3 on PLTR). box=[max(BollLower,Donch20Low), min(BollUpper,Donch20High)]. buy levels below price, sell above, inside box.
**Sizing — take the MIN of three caps:** shares_budget=floor(budget/price); shares_target=round((target+commission)/step); shares_risk=floor(equity*risk%/(stopmult*ATR)). shares=min(those three). Also total grid exposure Σ(shares*level_price) ≤ budget (trim levels if exceeded). Show all three so Henry sees why. (His mental model: budget $10k → $100 stock=100 shares, $50 stock=200 shares — the budget cap.)
**Entry confirm:** buy a level only if location (≤buy level / BollLower / S2–S3 / below VWAP by ~0.5–1×ATR) AND momentum (RSI(2)<10 OR Stoch %K crosses up 20). Exit: paired sell level / BollMid / above 5-day SMA. stop = entry − stopmult×ATR (and a hard stop below box).
**PAUSE switches (regime="trend", grid OFF) — ANY of:** close below box / above box; close<SMA200; ADX≥25; ATR>1.5× its 20-day avg; ≥3 closes outside same Bollinger band; earnings within 7d (unknown earnings date → PAUSE, fail-safe).
**Risk display:** daily max loss (dailystop) → 今日停手; averaging-down warning; base-rate honesty (≈35–50% of day traders profitable). Disclaimer on every output.

---

## §C — PIPELINE C: 大佬 (daily smart-money + influencers)
Tiers: 1 official (free, reliable) → 2 crowd → 3 influencers (optional). Build Tier 1 first.

**SEC EDGAR rules:** header `User-Agent: Hermes/1.0 REDACTED`; ≤10 req/s (sleep 0.12). Resolve CIK at runtime: `GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>&type=13F-HR&output=atom` → parse <CIK>. Cache.
**13F holdings:** `GET https://data.sec.gov/submissions/CIK##########.json` → find latest two `13F-HR` → fetch their informationTable XML in `https://www.sec.gov/Archives/edgar/data/<cik_int>/<accession_nodash>/` → parse {nameOfIssuer,cusip,value,shares}. Diff latest vs prior quarter: NEW / ADD(>+10%) / TRIM(<−10%) / EXIT. (45-day lag — timestamp it.)
**CUSIP→ticker:** OpenFIGI `POST https://api.openfigi.com/v3/mapping` body `[{"idType":"ID_CUSIP","idValue":"037833100","exchCode":"US"}]` (batch ≤10 no key / ≤100 with key; ~25 vs 250 req/min). Prefer common-stock/US match on multi-hit. Cache permanently in `cusip_ticker.json`. (Get the free key — cold cache for 5 funds is hundreds of CUSIPs.)
**Form 4 insider buys:** per company filings (form "4", last 1–2 days) → parse ownershipDocument `<nonDerivativeTransaction>` keep `transactionCode=="P"` (real purchases). Flag ≥2 distinct insiders in 14d = cluster (strong). Or scrape `http://openinsider.com/screener?...fd=7&xp=1` table as fallback. company CIK map: `https://www.sec.gov/files/company_tickers.json`.
**ARK:** download per-fund holdings CSV (paths change — verify live; fallback = trade-notification email parse; if 404 mark ark=false) → diff vs `cache/ark_<fund>_prev.csv`.
**Reddit:** `GET https://www.reddit.com/r/<sub>/hot.json?limit=100` with `User-Agent` header (or PRAW). Count $cashtags / ALL-CAPS tickers matching company_tickers.json (filter stopwords). Top 10 mentions.
**X (optional, paid):** resolve id `GET https://api.x.com/2/users/by/username/<h>` then `/2/users/<id>/tweets?max_results=20&exclude=retweets,replies` (Bearer). Or Nitter RSS fallback. Summarize via the single batched C LLM call. Everything Tier 3 tagged `unverified` + "讲自己的书" note.
**Crossref (the value):** intersect all signals with Henry's zone+watch tickers → plain headlines (timestamp each signal so 13F lag is visible). Output → `smart/{date}.json` + daily Telegram if crossref non-empty.

Tracked entities & honest reliability notes are in tracked_entities.json; key facts: Michael Burry deregistered Scion (2025-11) & sells a bearish newsletter — never mirror his shorts; `@aleabitoreddit`=Serenity, all his claims self-reported/unverified; `@LizThomasStrat` (not @LizYoungStrat). Official filings > influencers, always.

---

## §WEBSITE & SERVER
- Template = `hermes_dashboard_final.html` (4 tabs: 长投/大佬/做T/设置; responsive desktop+mobile; week picker for 长投, date pickers for 大佬/做T; in-site settings drawer split by the 3 strategies). Already built & verified — just feed it real JSON (§DATA) and host it.
- **FastAPI one process**, bind **127.0.0.1:8777**: serves `hermes_site/` static + `GET /api/config` (returns config.json so the drawer preloads) + `POST /api/config` (validate §CONFIG ranges, write). uvicorn as a service.

---

## §TELEGRAM (push + on-demand; one bot, restricted to TELEGRAM_CHAT_ID; always-on service, single-instance lock)
Scheduled: A weekly summary (Sat); C daily crossref (if any); B daily "今晚可做T…" (17:00 MYT).
On-demand commands (also accept natural-language equivalents):
| cmd | does | reply |
|---|---|---|
| `/help` | list | commands |
| `/zone` | 长投 IN_ZONE now (from latest value json) | `💰 入场区:\nGOOGL 现$182/推荐$165 🟢一时风向 ...` |
| `/watch SYM` | one stock vs its buy price | `GOOGL 现$182·推荐$165·还差10.3%·🟢基本面无恙` |
| `/dalao` | latest smart-money crossref + top moves | `★大佬(06-27):⭐GOOGL 3位高管集体买入(🟢官方)...` |
| `/dot` or `/swing` | LIVE recompute 做T pool, rank best | `现在适合做T(实时14:32):①PLTR $98.1🟢到买区$98→挂83股,卖$100,止损$92 ②AMD... ③NVDA🔴趋势 ④SNDK🔴财报` |
| `/add SYM` `/remove SYM` | edit swing_watchlist.json pins | `✓ 已加入做T池: SYM` |
Every reply ends `⚠ 仅供参考,非投资建议`. Unknown → /help.

---

## §OPS
Logging `logs/hermes_YYYYMMDD.log` (INFO, per fetch + counts + failures); any pipeline exception → Telegram error to Henry. Retry helper: 3 tries exp backoff, respect rate limits. Cache as §DISK. `journal/recommendations.csv`: log every recommendation (date,pipeline,ticker,action,price) and later score vs actual (长投 vs SPY; 做T fill/hit) — forward-only trust-building; universe is current-membership only (note survivorship bias; no backtest claims).

---

## §ACCEPTANCE (self-check)
- [ ] config.json round-trips: site edit → /api/config validates+writes → next run reads `config["long"/"swing"]`.
- [ ] swing_watchlist pins survive weekly pool refresh; Telegram /add /remove persist.
- [ ] value/smart/swing JSON populate on their cadences; site pickers load them; missing date → graceful message; NO mock data visible.
- [ ] Times correct MYT↔ET; NYSE holidays handled; "last session" never a non-trading day.
- [ ] ≤2 LLM calls/pipeline; B + website + writers = 0 LLM.
- [ ] Every tab + Telegram msg has disclaimer; server 127.0.0.1 only; no secret in served JSON.
- [ ] Unknown earnings → 做T paused; any source fail → 数据缺失, run completes, error pushed.
- [ ] zone/watch never contain Financials/Utilities; plain-language everywhere (no raw Z/F-score/z numbers shown).
- [ ] Build order followed: A0→A→C→B→server+bot.
```
