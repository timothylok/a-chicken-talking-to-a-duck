"""Generate gateway/public/index.html and CLAUDE.md's command list from COMMANDS.

Run by the pre-commit hook (ops/githooks/pre-commit) whenever asr/router.py
is committed, so the public home page and the CLAUDE.md "Current commands"
marker block always match COMMANDS. Output is deterministic: same COMMANDS ->
byte-identical files.

Run manually:  python ops/generate_homepage.py
"""

import html
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "asr"))

from router import COMMANDS, MACRO_DESCRIPTIONS  # noqa: E402

OUT_PATH = os.path.join(ROOT, "gateway", "public", "index.html")

# Spoken-Cantonese description per command ID; new commands fall back to a
# placeholder until a line is added here.
DESCRIPTIONS = {
    "SYSTEM_STATUS": "報告系統狀態：模型、設備、已運行幾耐",
    "LIST_COMMANDS": "讀出所有可用指令",
    "WEATHER_TODAY": "報告奧克蘭今日天氣：氣溫、天色、最高最低溫、落雨機會",
    "WEATHER_COMPARE": "同琴日嘅紀錄比較今日天氣：最高最低差幾多度（要有琴日紀錄先得）",
    "FUEL_PRICES": "報告附近最平嘅95汽油油站同價錢，最平排最先",
    "BUS_TIMES": "報告Glenfield Mall嚟緊嘅三班巴士：路線同幾多分鐘後開",
    "TIDE_TIMES": "報告奧克蘭下次潮漲潮退嘅時間同水位",
    "BIN_DAY": "報告屋企下次收垃圾、廚餘同回收嘅日子",
    "MILK_PRICES": "比較附近超市3公升標準牛奶價錢，最平排最先",
    "MORTGAGE_RATES": "比較五大銀行一年定息按揭利率，最平排最先",
    "EARTHQUAKES": "報告紐西蘭最近一次有感地震：幾耐之前、邊度、幾多級、幾深",
    "NEWS_HEADLINES": "用廣東話讀出紐西蘭今日三條頭條新聞（人名地名保留英文）",
    "JACKET_CHECK": "出門前檢查：而家有冇落雨、兩個鐘內會唔會落雨，話你知使唔使帶遮帶褸",
    "MORNING_BRIEFING": "一次過講晒：今日天氣、嚟緊嘅巴士、收垃圾提醒（今日或聽日先講）同三條新聞",
    "QUOTE_OF_DAY": "隨機講一句周星馳電影金句",
    "MOVIE_QUOTE": "隨機講一句港產片對白，會講埋戲名同角色",
    "SCHEDULE_TODAY": "讀出你Google日曆今日嘅安排：幾點、咩事，冇嘢就話你知冇安排",
    "CREATE_REMINDER": "喺你部iPhone加提醒事項：講「提我」加內容同時間（例：提我聽日朝早九點買牛奶）",
    "STOCK_ANALYSIS": "分析股票代號嘅現價、技術指標（RSI、平均線）同AI睇法：講「分析股票」加埋代號（例：分析股票 AAPL）——AI意見僅供參考，唔係投資建議",
    "POLYMARKET_ODDS": "查Polymarket預測市場對某件事嘅估計機會率：講「預測市場」加埋題目（例：預測市場 聯儲局減息）——係市場價錢，唔係事實",
    "PINE_INDICATOR": "本機AI生成TradingView Pine Script v5指標代碼（淨係Slack或者文字介面用得）：講「pine indicator:」加埋想要嘅指標邏輯——AI生成代碼未經TradingView編譯器驗證，只係草稿",
    "PINE_STRATEGY": "本機AI生成TradingView Pine Script v5回測策略代碼（淨係Slack或者文字介面用得）：講「pine strategy:」加埋想要嘅策略邏輯，固定用0.1%手續費、10%資金比例、一萬蚊本金——AI生成代碼未經TradingView編譯器驗證，只係草稿",
    "GENERATE_IMAGE": "本機AI畫圖（淨係Slack用得）：講「畫」加內容（例：畫 一隻太空貓），約一分鐘後張圖出現喺Slack",
    "RESTART_ASR": "重新啟動語音系統（約三十秒後恢復）",
}

# Home-page grouping (presentation only — the router knows no categories).
# New commands: add the ID to a group here as well as DESCRIPTIONS; anything
# unlisted falls into an automatic 其他 group so the hook never breaks.
CATEGORIES = [
    ("天氣出行", ["WEATHER_TODAY", "WEATHER_COMPARE", "JACKET_CHECK", "BUS_TIMES", "TIDE_TIMES"]),
    ("生活資訊", ["FUEL_PRICES", "BIN_DAY", "MILK_PRICES", "MORTGAGE_RATES", "STOCK_ANALYSIS", "POLYMARKET_ODDS", "PINE_INDICATOR", "PINE_STRATEGY", "EARTHQUAKES", "NEWS_HEADLINES"]),
    ("日程提醒", ["MORNING_BRIEFING", "SCHEDULE_TODAY", "CREATE_REMINDER"]),
    ("玩吓", ["QUOTE_OF_DAY", "MOVIE_QUOTE", "GENERATE_IMAGE"]),
    ("系統", ["SYSTEM_STATUS", "LIST_COMMANDS", "RESTART_ASR"]),
]

# Non-voice automations shown on the home page; the dashboard count derives
# from this list, so adding an automation = one entry here. The first field is
# the NZ wall-clock HH:MM the table is sorted by ("" = recurring, listed first),
# so a new entry lands in time order wherever it is added.
AUTOMATIONS = [
    ("10:00", "朝早十點", "iPhone自動攞當日簡報然後讀出嚟：天氣、巴士、收垃圾提醒、新聞"),
    ("09:00", "朝早九點", "檢查牛奶價錢，如果今日最平嘅3公升奶平過琴日，推送通知去手機"),
    ("10:05", "朝早10點05分", "監察價錢：PriceSpy上面嘅Nintendo Switch 2、Kingston Fury Beast Black DDR4記憶體、G.Skill Ripjaws V Black DDR4記憶體、Pokemon Pokopia (Switch 2)、Zelda: Tears of the Kingdom (Switch 2)、Zelda: Ocarina of Time (Switch 2)、Asus GeForce RTX 5050顯示卡、Nintendo Switch 2薩爾達40週年特別版主機；Trade Me上面嘅Nintendo Switch 2；仲有Bottle-O同Super Liquor嘅Aberlour 12年威士忌。只計有貨嘅價錢，平過或者貴過上次記錄最少1%就推送通知去手機；缺貨嘅貨品一返貨都會即刻通知"),
    ("", "每個鐘", "系統心跳檢查 — 條通道或者語音服務死咗，手機即刻收到高優先通知"),
    ("", "每五分鐘", "指令紀錄自動同步去Notion（傾偈內容唔會離開屋企部機）"),
    ("04:32", "凌晨4點32分", "自動清理舊紀錄：傾偈內容留30日，系統日誌留90日，指令紀錄長期保存"),
    ("", "每分鐘", "檢查提醒事項，到咗指定時間就推送通知去手機"),
    ("06:20", "朝早6點20分", "落雨機率高過7成就推送通知提你帶遮"),
    ("", "每分鐘", "執行自訂工作流規則：聽日收垃圾今晚提你、指令出錯即刻通知"),
    ("", "每十五分鐘", "同步Google日曆，淨係攞今日同聽日嘅行程標題同時間，俾「今日行程」指令用"),
    ("09:00", "朝早九點", "收集NVIDIA相關新聞，本機AI生成每日報告草稿"),
    ("08:15", "朝早8點15分", "監察香港直飛奧克蘭來回機票（國泰／紐航，經濟艙，一位成人）：12月26至31號出發、玩14至21日，每日喺Google Flights搜齊48個日期組合，用港幣記錄，生成價格日曆報告；任何組合平過琴日10%或者創新低就推送通知去手機"),
    ("07:30", "朝早7點半", "整理每日AI精選：Google News嘅AI新聞同OpenAI、Anthropic狀態頁嘅故障，每條都連返原文，放上 /dashboard/ai"),
]

# Stock-related automations get their own visual timeline on the home page
# (see STOCK_TIMELINE_INTRO + the .timeline CSS/render logic below) instead
# of living in the generic AUTOMATIONS table above — seven real scheduled
# tasks plus Category 1's reactive refresh, in actual chronological order.
# Times/scripts confirmed live via `Get-ScheduledTask` 2026-09-29 — keep in
# sync if a task's trigger time ever changes. The triggers are anchored to a
# UTC offset, so their NZ wall-clock time moves an hour at each DST change;
# these are NZDT (daylight) times.
STOCK_TIMELINE = [
    ("02:00", "凌晨", "Category 2", "業績監察 Earnings Watch",
     "監察定咗嗰啲股票嘅SEC新聞稿（8-K），一有新季度業績就自動生成分析報告——EPS對比市場預期、前瞻指引、四季分部趨勢（加速定減速由程式計，唔係AI估）——寫落Notion；文字分析由Cloudflare Workers AI生成，失敗就自動轉返屋企部機嘅本機AI"),
    ("02:30", "凌晨", "Category 3", "估值分析 Valuation Watch",
     "同一個觸發條件，自動跑齊DCF現金流折現、倍數法、反推隱含增長率、同業比較等多種估值模型，寫落Notion；文字分析同樣由Cloudflare Workers AI生成，失敗就轉返本機AI"),
    ("03:00", "凌晨", "Category 5", "風險紅旗 Risk Watch",
     "監察定咗嗰啲股票有冇新年報（10-K），揪出主要風險因素、資產負債表外負債、商譽減值、應收帳款同存貨趨勢等鑑證式分析，寫落Notion；文字分析同樣由Cloudflare Workers AI生成，失敗就轉返本機AI"),
    ("即時", "觸發", "Category 1", "基本面快照刷新",
     "Category 2／3／5 一有新報告成功生成，即刻觸發，重新整理返嗰隻股票嘅基本面快照（現價、時效標記），確保資料新鮮"),
    ("10:30", "朝早", "Category 4", "技術分析 Technicals Daily",
     "讀週線同日線走勢圖、成交量、對大盤（SPY）強弱、業績波幅，每日生成技術分析報告寫落Notion；文字分析由Cloudflare Workers AI（Llama 3.3 70B）生成，快好多之餘唔再佔用屋企部機嘅GPU"),
    ("11:00", "朝早", "—", "業績報告通知",
     "如果凌晨嗰輪監察到新嘅季度業績報告，推送手機通知話你知——刻意同生成分開幾個鐘，唔會半夜嘈醒你"),
    ("11:10", "朝早", "Category 6", "風險評分儀表板 Risk Dashboard",
     "計算10個KPI風險評分（0-1分同紅黃綠燈），三句總結由Cloudflare Workers AI生成，寫落Notion，然後自動出版去<a href=\"/dashboard\">網頁儀表板</a>——生成失敗就唔會出版，唔會俾舊儀表板落線"),
    ("11:25", "朝早", "—", "每日股價範圍 Stock Day Range",
     "即時攞Yahoo Finance數據計50/200日均線、RSI、支持阻力位，加上20日歷史波幅推算出嘅預期浮動範圍，再讀返Category 6嘅風險評分做參考，Cloudflare Workers AI寫低每隻股票嘅簡短分析——純粹技術/波幅參考，唔係價錢預測，都唔係投資建議，存做本機HTML報告"),
    ("11:45", "朝早", "—", "Polymarket 賠率監察",
     "睇Polymarket預測市場對Mag 7（NVDA、MSFT、GOOGL、AAPL、AMZN、META、TSLA）嘅賠率：全球最大公司、業績勝預期、本月股價、市值，每日自動搵返最新嘅盤；11月8號之前仲會睇埋紐西蘭大選（最多議席、執政聯盟、總理、各黨議席、投票率等）；賠率比上次記錄郁得多先推送通知去手機，新業績盤一開都會通知——係市場價錢，唔係預測，都唔係投資建議"),
]

# Pantone Colors of the Year as section accents: (code, name, approximate sRGB).
# Panels and command categories each get their own; a category missing here
# (e.g. a new one) falls back to Ultimate Gray rather than breaking the hook.
PANTONE = {
    "about": ("19-4052", "Classic Blue", "#0F4C81"),
    "commands": ("17-3938", "Very Peri", "#6667AB"),
    "automations": ("17-5641", "Emerald", "#009473"),
    "stocks": ("18-1750", "Viva Magenta", "#BB2649"),
}
CATEGORY_PANTONE = {
    "天氣出行": ("15-5217", "Blue Turquoise", "#53B0AE"),
    "生活資訊": ("17-1463", "Tangerine Tango", "#DD4124"),
    "日程提醒": ("17-1230", "Mocha Mousse", "#A47864"),
    "玩吓": ("16-1546", "Living Coral", "#FF6F61"),
    "系統": ("17-5104", "Ultimate Gray", "#939597"),
    "其他": ("15-0343", "Greenery", "#88B04B"),
}
_FALLBACK_PANTONE = ("17-5104", "Ultimate Gray", "#939597")

PAGE = """<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>雞同鴨講 — 廣東話語音OS</title>
<style>
  :root {{ --bg: #ffffff; --card: #ffffff; --fg: #1a1a1a; --muted: #5f6368; --line: #e3e3e3;
          --ink-mix: #000; --ink-pct: 70%; --tint-pct: 10%; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #16181d; --card: #1d2026; --fg: #e8e8e8; --muted: #a0a4ab; --line: #30343b;
            --ink-mix: #fff; --ink-pct: 62%; --tint-pct: 18%; }}
  }}
  /* Every coloured block sets --c (its Pantone); ink/tint derive from it so
     text keeps contrast in both themes. */
  [style*="--c"] {{ --ink: color-mix(in srgb, var(--c) var(--ink-pct), var(--ink-mix));
                   --tint: color-mix(in srgb, var(--c) var(--tint-pct), var(--bg)); }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0 auto; max-width: 62rem; padding: 1.5rem 1rem 4rem;
         background: var(--bg); color: var(--fg);
         font-family: -apple-system, "PingFang HK", "Microsoft JhengHei", sans-serif;
         line-height: 1.7; }}
  a {{ color: var(--ink, inherit); }}
  .top {{ display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 1rem; }}
  h1 {{ font-size: 1.7rem; margin: 0; line-height: 1.2; }}
  .tagline {{ color: var(--muted); margin: 0.2rem 0 0; }}
  .translate {{ font-size: 0.8rem; margin: 0.2rem 0 0; }}
  .translate a {{ color: var(--muted); }}
  .stats {{ display: flex; gap: 0.6rem; }}
  .stat {{ border: 1px solid var(--line); border-top: 4px solid var(--c); border-radius: 10px;
          padding: 0.35rem 0.9rem; text-align: center; text-decoration: none; color: var(--fg);
          background: var(--card); min-width: 5.5rem; }}
  .stat .num {{ display: block; font-size: 1.5rem; font-weight: 700; color: var(--ink); line-height: 1.2; }}
  .stat .label {{ color: var(--muted); font-size: 0.8rem; }}
  .nav {{ display: flex; flex-wrap: wrap; gap: 0.4rem 1.1rem; margin: 0.9rem 0 0; font-size: 0.95rem; }}
  .nav a {{ font-weight: 600; text-decoration: none; color: var(--fg); }}
  .nav a:hover {{ text-decoration: underline; }}
  .swatch {{ display: inline-flex; align-items: center; gap: 0.4rem; font-size: 0.68rem;
            letter-spacing: 0.02em; color: var(--muted); white-space: nowrap; font-weight: 500; }}
  .swatch i {{ width: 0.9rem; height: 0.9rem; border-radius: 3px; background: var(--c); }}
  .about {{ margin-top: 1.25rem; background: var(--tint); border-left: 5px solid var(--c);
           border-radius: 10px; padding: 0.9rem 1.1rem; }}
  .about p {{ margin: 0 0 0.5rem; }}
  .about .head {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 0.5rem;
                 align-items: baseline; }}
  .about h2 {{ font-size: 1.05rem; margin: 0 0 0.35rem; color: var(--ink); }}
  .flow {{ color: var(--muted); font-size: 0.85rem; overflow-x: auto; white-space: nowrap; margin: 0; }}
  .tabs {{ position: sticky; top: 0; z-index: 5; display: flex; gap: 0.4rem; overflow-x: auto;
          margin: 1.5rem -1rem 0; padding: 0.6rem 1rem; background: var(--bg);
          border-bottom: 1px solid var(--line); }}
  .tab {{ flex: none; text-decoration: none; color: var(--fg); font-weight: 600; font-size: 0.95rem;
         border: 1px solid var(--line); border-bottom: 3px solid var(--c); border-radius: 8px;
         padding: 0.3rem 0.9rem; background: var(--card); }}
  .tab .n {{ color: var(--muted); font-weight: 500; font-size: 0.8rem; margin-left: 0.3rem; }}
  .tab.active {{ background: var(--c); color: #fff; border-color: var(--c); }}
  .tab.active .n {{ color: rgba(255,255,255,0.85); }}
  .panel {{ padding-top: 0.5rem; scroll-margin-top: 4rem; }}
  .panel-head {{ display: flex; flex-wrap: wrap; justify-content: space-between; align-items: baseline;
                gap: 0.5rem; margin: 1rem 0 0.25rem; border-bottom: 3px solid var(--c); padding-bottom: 0.3rem; }}
  .panel-head h2 {{ font-size: 1.15rem; margin: 0; color: var(--ink); }}
  .note {{ color: var(--muted); font-size: 0.85rem; margin: 0.5rem 0 0; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 0.45rem; margin-top: 0.9rem; }}
  .chip {{ border: 1px solid var(--line); border-radius: 999px; background: var(--card);
          color: var(--fg); font: inherit; font-size: 0.88rem; padding: 0.15rem 0.8rem;
          cursor: pointer; display: inline-flex; align-items: center; gap: 0.35rem; }}
  .chip::before {{ content: ""; width: 0.6rem; height: 0.6rem; border-radius: 50%; background: var(--c, var(--fg)); }}
  .chip .n {{ color: var(--muted); font-size: 0.75rem; }}
  .chip.active {{ border-color: var(--c, var(--fg)); background: var(--tint, var(--card)); font-weight: 700; }}
  .cmd-group {{ margin-top: 1.4rem; }}
  .group-head {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.6rem; margin-bottom: 0.6rem; }}
  .group-head h3 {{ font-size: 1rem; margin: 0; color: var(--ink); }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(16.5rem, 1fr)); gap: 0.7rem; }}
  .card {{ background: var(--card); border: 1px solid var(--line); border-top: 4px solid var(--c);
          border-radius: 10px; padding: 0.7rem 0.9rem; min-width: 0; }}
  .card .phrase {{ font-weight: 700; font-size: 1.05rem; color: var(--ink); }}
  .card .desc {{ margin: 0.25rem 0 0; font-size: 0.9rem; }}
  .card details {{ margin-top: 0.4rem; font-size: 0.8rem; color: var(--muted); }}
  .card summary {{ cursor: pointer; }}
  .confirm {{ color: var(--ink); font-size: 0.8rem; }}
  .when {{ display: inline-block; font-weight: 700; font-size: 0.8rem; color: #fff; background: var(--c);
          border-radius: 999px; padding: 0 0.6rem; }}
  .clamp {{ display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden; }}
  .clamp.open {{ display: block; }}
  .more {{ border: none; background: none; padding: 0; margin-top: 0.15rem; font: inherit;
          font-size: 0.8rem; color: var(--ink); cursor: pointer; font-weight: 600; }}
  .timeline {{ display: flex; flex-direction: column; margin-top: 1rem; }}
  .t-row {{ display: flex; gap: 0.9rem; }}
  .t-time {{ flex: 0 0 3.4rem; text-align: right; font-weight: 700; color: var(--ink);
            font-size: 0.85rem; padding-top: 0.15rem; white-space: nowrap; }}
  .t-rail {{ flex: 0 0 0.7rem; display: flex; flex-direction: column; align-items: center; }}
  .t-dot {{ width: 0.7rem; height: 0.7rem; border-radius: 50%; background: var(--c); flex: none; margin-top: 0.3rem; }}
  .t-line {{ flex: 1; width: 2px; background: var(--tint); margin-top: 0.15rem; }}
  .t-row:last-child .t-line {{ display: none; }}
  .t-card {{ flex: 1; padding-bottom: 1.1rem; min-width: 0; }}
  .t-head {{ display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; }}
  .t-cat {{ font-size: 0.7rem; font-weight: 700; color: #fff; background: var(--c);
           border-radius: 999px; padding: 0.05rem 0.55rem; white-space: nowrap; }}
  .t-name {{ font-weight: 600; font-size: 0.95rem; }}
  .t-desc {{ margin: 0.15rem 0 0; color: var(--muted); font-size: 0.85rem; }}
  footer {{ margin-top: 2.5rem; color: var(--muted); font-size: 0.8rem;
           border-top: 1px solid var(--line); padding-top: 1rem; }}
  @media (max-width: 30rem) {{ h1 {{ font-size: 1.45rem; }} .stat {{ min-width: 0; padding: 0.3rem 0.6rem; }} }}
</style>
</head>
<body>
<header class="top">
<div>
<h1>雞同鴨講</h1>
<p class="tagline">私人廣東話語音OS</p>
<p class="translate"><a href="https://translate.google.com/translate?sl=auto&amp;tl=en&amp;u=https://a-chicken-talking-to-a-duck.vercel.app/" rel="nofollow">Translate to English (Google Translate)</a></p>
</div>
<div class="stats">
  <a class="stat" href="#commands" data-tab="commands" style="--c:{c_commands}"><span class="num">{command_count}</span><span class="label">語音指令</span></a>
  <a class="stat" href="#automations" data-tab="automations" style="--c:{c_automations}"><span class="num">{automation_count}</span><span class="label">自動功能</span></a>
  <a class="stat" href="#stocks" data-tab="stocks" style="--c:{c_stocks}"><span class="num">{stock_count}</span><span class="label">股票程序</span></a>
</div>
</header>
<nav class="nav">
<a href="/dashboard">📊 風險儀表板</a>
<a href="/dashboard/ai">🤖 AI精選</a>
<a href="/chat.html">💬 打字版傾偈</a>
<a href="/snake.html">🐍 食蛇</a>
</nav>

<section class="about" style="--c:{c_about}">
<div class="head"><h2>呢個係乜嘢嚟？</h2>{sw_about}</div>
<p>一個自己屋企自己搞掂嘅語音助手。喺iPhone對住個捷徑講廣東話，段聲音經加密通道送返屋企部Windows機，
用本地模型認出你講乜再執行指令；唔係指令嘅嘢就交畀本地AI同你傾偈。語音同文字都留喺自己機度做辨識，
唔會送去第三方雲端AI，指令要一字不差先會執行。</p>
<p class="flow">iPhone 🎤 → Vercel → Cloudflare Tunnel → 屋企Win11（語音辨識）→ 指令／AI傾偈 → 講返畀你聽</p>
</section>

<nav class="tabs" aria-label="分頁">
<a class="tab" href="#commands" data-tab="commands" style="--c:{c_commands}">指令一覽<span class="n">{command_count}</span></a>
<a class="tab" href="#automations" data-tab="automations" style="--c:{c_automations}">自動功能<span class="n">{automation_count}</span></a>
<a class="tab" href="#stocks" data-tab="stocks" style="--c:{c_stocks}">股票時間表<span class="n">{stock_count}</span></a>
</nav>

<section class="panel" id="commands" style="--c:{c_commands}">
<div class="panel-head"><h2>指令一覽</h2>{sw_commands}</div>
<div class="filters">
{filter_chips}
</div>
{command_groups}
<p class="note">危險指令會先讀返你嘅指令出嚟，六十秒之內講「<strong>確認</strong>」先會執行，講「<strong>取消</strong>」就唔做。講其他嘢？唔使指令，直接問 — 本地AI會用廣東話答你。</p>
</section>

<section class="panel" id="automations" style="--c:{c_automations}">
<div class="panel-head"><h2>自動功能（唔使出聲）</h2>{sw_automations}</div>
<div class="cards" style="margin-top:1rem">
{automation_cards}
</div>
</section>

<section class="panel" id="stocks" style="--c:{c_stocks}">
<div class="panel-head"><h2>股票分析自動化時間表</h2>{sw_stocks}</div>
<p class="note">股票相關程序由凌晨到朝早自動接力執行，資料寫落Notion</p>
<div class="timeline">
{stock_timeline}
</div>
</section>

<footer>私人系統：所有指令都要有授權金鑰先用得。呢頁由 asr/router.py 嘅指令表自動生成。顏色取自歷年 PANTONE Color of the Year（近似值）。</footer>
<script>
// Progressive enhancement: without JS every panel shows and the tabs are
// plain jump links; with it, one panel at a time.
const panels = [...document.querySelectorAll(".panel")];
const tabLinks = [...document.querySelectorAll("[data-tab]")];
function addMoreButtons(root) {{
  root.querySelectorAll(".clamp:not([data-checked])").forEach((el) => {{
    el.dataset.checked = "1";
    if (el.scrollHeight <= el.clientHeight + 2) return;
    const btn = document.createElement("button");
    btn.className = "more"; btn.type = "button"; btn.textContent = "展開 ▾";
    btn.addEventListener("click", () => {{
      const open = el.classList.toggle("open");
      btn.textContent = open ? "收埋 ▴" : "展開 ▾";
    }});
    el.after(btn);
  }});
}}
function show(id) {{
  if (!panels.some((p) => p.id === id)) id = "commands";
  panels.forEach((p) => {{ p.hidden = p.id !== id; }});
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === id));
  addMoreButtons(document.getElementById(id));
}}
tabLinks.forEach((a) => a.addEventListener("click", (e) => {{
  e.preventDefault();
  history.replaceState(null, "", "#" + a.dataset.tab);
  show(a.dataset.tab);
  document.querySelector(".tabs").scrollIntoView({{ block: "start" }});
}}));
show(location.hash.slice(1));

const chips = document.querySelectorAll(".chip");
chips.forEach((chip) => chip.addEventListener("click", () => {{
  chips.forEach((c) => c.classList.toggle("active", c === chip));
  const cat = chip.dataset.cat;
  document.querySelectorAll(".cmd-group").forEach((g) => {{
    g.style.display = cat === "all" || g.dataset.cat === cat ? "" : "none";
  }});
}}));
</script>
</body>
</html>
"""


def _swatch(pantone: tuple) -> str:
    code, name, hex_ = pantone
    return f'<span class="swatch" style="--c:{hex_}"><i></i>PANTONE {code} {html.escape(name)}</span>'


def _command_card(command_id: str) -> str:
    spec = COMMANDS[command_id]
    primary, *alts = spec["phrases"]
    desc = DESCRIPTIONS.get(command_id) or MACRO_DESCRIPTIONS.get(command_id) or "（未有說明）"
    if spec["destructive"]:
        desc += '<div class="confirm">⚠ 要講「確認」先執行</div>'
    others = (f"<details><summary>其他講法（{len(alts)}）</summary>{html.escape('、'.join(alts))}</details>"
              if alts else "")
    return (
        '<article class="card">'
        f'<div class="phrase">{html.escape(primary)}</div>'
        f'<div class="desc clamp">{desc}</div>'
        f"{others}</article>"
    )


def render() -> str:
    categorized = {cid for _, ids in CATEGORIES for cid in ids}
    leftover = [cid for cid in COMMANDS if cid not in categorized]
    groups_spec = [(name, [cid for cid in ids if cid in COMMANDS]) for name, ids in CATEGORIES]
    if leftover:
        groups_spec.append(("其他", leftover))

    chips = ['<button class="chip active" data-cat="all">全部</button>']
    groups = []
    for name, ids in groups_spec:
        if not ids:
            continue
        pantone = CATEGORY_PANTONE.get(name, _FALLBACK_PANTONE)
        esc = html.escape(name)
        chips.append(f'<button class="chip" data-cat="{esc}" style="--c:{pantone[2]}">'
                     f'{esc}<span class="n">{len(ids)}</span></button>')
        cards = "\n".join(_command_card(cid) for cid in ids)
        groups.append(
            f'<section class="cmd-group" data-cat="{esc}" style="--c:{pantone[2]}">\n'
            f'<div class="group-head"><h3>{esc}</h3>{_swatch(pantone)}</div>\n'
            f'<div class="cards">\n{cards}\n</div>\n</section>'
        )

    automation_cards = [
        f'<article class="card"><span class="when">{html.escape(when)}</span>'
        f'<p class="desc clamp">{html.escape(what)}</p></article>'
        for _, when, what in sorted(AUTOMATIONS, key=lambda a: a[0])
    ]

    timeline_rows = [
        "<div class=\"t-row\">\n"
        f'<div class="t-time">{html.escape(time)}<br>{html.escape(period)}</div>\n'
        '<div class="t-rail"><span class="t-dot"></span><span class="t-line"></span></div>\n'
        '<div class="t-card">\n'
        '<div class="t-head">'
        + (f'<span class="t-cat">{html.escape(cat)}</span>' if cat != "—" else "")
        + f'<span class="t-name">{html.escape(name)}</span>'
        "</div>\n"
        f'<p class="t-desc clamp">{desc}</p>\n'
        "</div>\n</div>"
        for time, period, cat, name, desc in STOCK_TIMELINE
    ]

    colours = {f"c_{k}": v[2] for k, v in PANTONE.items()}
    swatches = {f"sw_{k}": _swatch(v) for k, v in PANTONE.items()}
    return PAGE.format(
        filter_chips="\n".join(chips),
        command_groups="\n".join(groups),
        automation_cards="\n".join(automation_cards),
        stock_timeline="\n".join(timeline_rows),
        command_count=len(COMMANDS),
        automation_count=len(AUTOMATIONS),
        stock_count=len(STOCK_TIMELINE),
        **colours, **swatches,
    )


CLAUDE_MD = os.path.join(ROOT, "CLAUDE.md")
MARK_BEGIN, MARK_END = "<!-- COMMANDS:BEGIN -->", "<!-- COMMANDS:END -->"


def sync_claude_md() -> None:
    items = []
    for command_id, spec in COMMANDS.items():
        suffix = "，destructive" if spec["destructive"] else ""
        items.append(f"`{command_id}` ({spec['phrases'][0]}{suffix})")
    with open(CLAUDE_MD, encoding="utf-8") as f:
        text = f.read()
    head, rest = text.split(MARK_BEGIN, 1)
    _, tail = rest.split(MARK_END, 1)
    with open(CLAUDE_MD, "w", encoding="utf-8", newline="\n") as f:
        f.write(head + MARK_BEGIN + "、".join(items) + MARK_END + tail)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render())
    sync_claude_md()
    print(f"wrote {os.path.relpath(OUT_PATH, ROOT)} + CLAUDE.md commands ({len(COMMANDS)} commands)")
