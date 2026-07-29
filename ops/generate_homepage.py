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

from router import COMMANDS  # noqa: E402

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
    "CREATE_REMINDER": "喺你部iPhone加提醒事項：講「提我」加內容同時間（例：提我聽日朝早九點買牛奶）",
    "STOCK_ANALYSIS": "分析股票代號嘅現價、技術指標（RSI、平均線）同AI睇法：講「分析股票」加埋代號（例：分析股票 AAPL）——AI意見僅供參考，唔係投資建議",
    "PINE_INDICATOR": "本機AI生成TradingView Pine Script v5指標代碼（淨係Slack或者文字介面用得）：講「pine indicator:」加埋想要嘅指標邏輯——AI生成代碼未經TradingView編譯器驗證，只係草稿",
    "PINE_STRATEGY": "本機AI生成TradingView Pine Script v5回測策略代碼（淨係Slack或者文字介面用得）：講「pine strategy:」加埋想要嘅策略邏輯，固定用0.1%手續費、10%資金比例、一萬蚊本金——AI生成代碼未經TradingView編譯器驗證，只係草稿",
    "GENERATE_IMAGE": "本機AI畫圖（淨係Slack用得）：講「畫」加內容（例：畫 一隻太空貓），約一分鐘後張圖出現喺Slack",
    "RESTART_ASR": "重新啟動語音系統（約三十秒後恢復）",
    "TRIGGER_DEPLOY": "重新部署網站",
}

# Home-page grouping (presentation only — the router knows no categories).
# New commands: add the ID to a group here as well as DESCRIPTIONS; anything
# unlisted falls into an automatic 其他 group so the hook never breaks.
CATEGORIES = [
    ("天氣出行", ["WEATHER_TODAY", "WEATHER_COMPARE", "JACKET_CHECK", "BUS_TIMES", "TIDE_TIMES"]),
    ("生活資訊", ["FUEL_PRICES", "BIN_DAY", "MILK_PRICES", "MORTGAGE_RATES", "STOCK_ANALYSIS", "PINE_INDICATOR", "PINE_STRATEGY", "EARTHQUAKES", "NEWS_HEADLINES"]),
    ("日程提醒", ["MORNING_BRIEFING", "CREATE_REMINDER"]),
    ("玩吓", ["QUOTE_OF_DAY", "MOVIE_QUOTE", "GENERATE_IMAGE"]),
    ("系統", ["SYSTEM_STATUS", "LIST_COMMANDS", "RESTART_ASR", "TRIGGER_DEPLOY"]),
]

# Non-voice automations shown on the home page; the dashboard count derives
# from this list, so adding an automation = one entry here.
AUTOMATIONS = [
    ("朝早十點", "iPhone自動攞當日簡報然後讀出嚟：天氣、巴士、收垃圾提醒、新聞"),
    ("朝早九點", "檢查牛奶價錢，如果今日最平嘅3公升奶平過琴日，推送通知去手機"),
    ("朝早9點05分", "監察PriceSpy定咗嗰啲產品價錢，如果今日平過上次記錄就推送通知去手機"),
    ("每十分鐘", "系統心跳檢查 — 條通道或者語音服務死咗，手機即刻收到高優先通知"),
    ("每五分鐘", "指令紀錄自動同步去Notion（傾偈內容唔會離開屋企部機）"),
    ("朝早三點半", "自動清理舊紀錄：傾偈內容留30日，系統日誌留90日，指令紀錄長期保存"),
    ("每分鐘", "檢查提醒事項，到咗指定時間就推送通知去手機"),
    ("朝早6點20分", "落雨機率高過7成就推送通知提你帶遮"),
    ("每分鐘", "執行自訂工作流規則：聽日收垃圾今晚提你、指令出錯即刻通知"),
]

# Stock-related automations get their own visual timeline on the home page
# (see STOCK_TIMELINE_INTRO + the .timeline CSS/render logic below) instead
# of living in the generic AUTOMATIONS table above — six real scheduled
# tasks plus Category 1's reactive refresh, in actual chronological order.
# Times/scripts confirmed live via `Get-ScheduledTask` 2026-07-29 — keep in
# sync if a task's trigger time ever changes.
STOCK_TIMELINE = [
    ("01:00", "凌晨", "Category 2", "業績監察 Earnings Watch",
     "監察定咗嗰啲股票嘅SEC新聞稿（8-K），一有新季度業績就自動生成分析報告——EPS對比市場預期、前瞻指引、四季分部趨勢——寫落Notion"),
    ("01:30", "凌晨", "Category 3", "估值分析 Valuation Watch",
     "同一個觸發條件，自動跑齊DCF現金流折現、倍數法、反推隱含增長率、同業比較等多種估值模型，寫落Notion"),
    ("02:00", "凌晨", "Category 5", "風險紅旗 Risk Watch",
     "監察定咗嗰啲股票有冇新年報（10-K），揪出主要風險因素、資產負債表外負債、商譽減值、應收帳款同存貨趨勢等鑑證式分析，寫落Notion"),
    ("即時", "觸發", "Category 1", "基本面快照刷新",
     "Category 2／3／5 一有新報告成功生成，即刻觸發，重新整理返嗰隻股票嘅基本面快照（現價、時效標記），確保資料新鮮"),
    ("09:30", "朝早", "Category 4", "技術分析 Technicals Daily",
     "讀週線同日線走勢圖、成交量、對大盤（SPY）強弱、業績波幅，每日生成技術分析報告寫落Notion"),
    ("10:00", "朝早", "Category 6", "風險評分儀表板 Risk Dashboard",
     "計算10個KPI風險評分（0-1分同紅黃綠燈），寫落Notion，然後自動出版去<a href=\"/dashboard\">網頁儀表板</a>——生成失敗就唔會出版，唔會俾舊儀表板落線"),
    ("10:00", "朝早", "—", "業績報告通知",
     "如果凌晨嗰輪監察到新嘅季度業績報告，推送手機通知話你知——刻意同生成分開幾個鐘，唔會半夜嘈醒你"),
]

PAGE = """<!DOCTYPE html>
<html lang="zh-HK">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>雞同鴨講 — 廣東話語音OS</title>
<style>
  :root {{ --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e0e0e0; --accent: #b45309; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #16181d; --fg: #e8e8e8; --muted: #9a9a9a; --line: #333; --accent: #f59e0b; }}
  }}
  body {{ margin: 0 auto; max-width: 44rem; padding: 2rem 1.25rem 4rem;
         background: var(--bg); color: var(--fg);
         font-family: -apple-system, "PingFang HK", "Microsoft JhengHei", sans-serif;
         line-height: 1.75; }}
  h1 {{ font-size: 1.6rem; margin-bottom: 0.25rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2.5rem; border-bottom: 1px solid var(--line);
       padding-bottom: 0.35rem; }}
  .tagline {{ color: var(--muted); margin-top: 0; }}
  .translate {{ font-size: 0.85rem; margin-top: 0.25rem; }}
  .translate a {{ color: var(--muted); }}
  .nav {{ display: flex; gap: 1.25rem; margin-top: 1rem; }}
  .nav a {{ color: var(--accent); font-weight: 600; text-decoration: none; }}
  .nav a:hover {{ text-decoration: underline; }}
  .flow {{ color: var(--muted); font-size: 0.9rem; overflow-x: auto; white-space: nowrap; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }}
  th {{ font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  .phrase {{ font-weight: 600; white-space: nowrap; }}
  .alt {{ color: var(--muted); font-size: 0.85rem; }}
  .confirm {{ color: var(--accent); font-size: 0.85rem; white-space: nowrap; }}
  .filters {{ display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 1rem; }}
  .chip {{ border: 1px solid var(--line); border-radius: 999px; background: none;
          color: var(--fg); font: inherit; font-size: 0.9rem; padding: 0.2rem 0.85rem;
          cursor: pointer; }}
  .chip.active {{ border-color: var(--accent); color: var(--accent); font-weight: 600; }}
  .cmd-group h3 {{ font-size: 1rem; margin: 1.75rem 0 0; color: var(--muted); }}
  .cmd-group table {{ margin-top: 0.5rem; }}
  .stats {{ display: flex; gap: 1rem; margin-top: 1.5rem; }}
  .stat {{ flex: 1; border: 1px solid var(--line); border-radius: 8px;
          padding: 0.75rem 1rem; text-align: center; }}
  .stat .num {{ font-size: 2rem; font-weight: 700; color: var(--accent); line-height: 1.2; }}
  .stat .label {{ color: var(--muted); font-size: 0.85rem; }}
  footer {{ margin-top: 3rem; color: var(--muted); font-size: 0.8rem;
           border-top: 1px solid var(--line); padding-top: 1rem; }}
  h3.timeline-title {{ font-size: 1rem; margin: 2rem 0 0.15rem; }}
  .timeline-caption {{ color: var(--muted); font-size: 0.85rem; margin: 0 0 1.25rem; }}
  .timeline {{ display: flex; flex-direction: column; }}
  .t-row {{ display: flex; gap: 0.9rem; }}
  .t-time {{ flex: 0 0 3.4rem; text-align: right; font-weight: 700;
            color: var(--accent); font-size: 0.85rem; padding-top: 0.15rem;
            white-space: nowrap; }}
  .t-rail {{ flex: 0 0 0.7rem; display: flex; flex-direction: column; align-items: center; }}
  .t-dot {{ width: 0.65rem; height: 0.65rem; border-radius: 50%;
           background: var(--accent); flex: none; margin-top: 0.22rem; }}
  .t-line {{ flex: 1; width: 2px; background: var(--line); margin-top: 0.15rem; }}
  .t-row:last-child .t-line {{ display: none; }}
  .t-card {{ flex: 1; padding-bottom: 1.4rem; }}
  .t-head {{ display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.2rem; }}
  .t-cat {{ font-size: 0.72rem; font-weight: 700; color: var(--bg); background: var(--accent);
           border-radius: 999px; padding: 0.05rem 0.55rem; white-space: nowrap; }}
  .t-name {{ font-weight: 600; font-size: 0.95rem; }}
  .t-desc {{ margin: 0; color: var(--muted); font-size: 0.85rem; }}
  .t-desc a {{ color: var(--accent); }}
</style>
</head>
<body>
<h1>雞同鴨講</h1>
<p class="tagline">私人廣東話語音OS</p>
<p class="translate"><a href="https://translate.google.com/translate?sl=auto&amp;tl=en&amp;u=https://a-chicken-talking-to-a-duck.vercel.app/" rel="nofollow">Translate to English (Google Translate)</a></p>
<nav class="nav">
<a href="/dashboard">📊 風險儀表板</a>
<a href="/chat.html">💬 打字版傾偈</a>
</nav>

<div class="stats">
  <div class="stat"><div class="num">{command_count}</div><div class="label">語音指令</div></div>
  <div class="stat"><div class="num">{automation_count}</div><div class="label">自動功能</div></div>
</div>

<h2>呢個係乜嘢嚟？</h2>
<p>一個自己屋企自己搞掂嘅語音助手。喺iPhone對住個捷徑講廣東話，
段聲音會經加密通道送返屋企部Windows機，用本地模型認出你講乜，
再執行指令；唔係指令嘅嘢就交畀本地AI同你傾偈。</p>
<p>成個過程語音同文字都留喺自己機度做辨識，唔會送去第三方雲端AI，
指令仲要一字不差先會執行，危險動作重要講「確認」先做。</p>
<p class="flow">iPhone 🎤 → Vercel → Cloudflare Tunnel → 屋企Win11（語音辨識）→ 指令／AI傾偈 → 講返畀你聽</p>

<h2>指令一覽</h2>
<div class="filters">
{filter_chips}
</div>
{command_groups}
<p>危險指令會先讀返你嘅指令出嚟，六十秒之內講「<strong>確認</strong>」先會執行，講「<strong>取消</strong>」就唔做。</p>
<p>講其他嘢？唔使指令，直接問 — 本地AI會用廣東話答你。</p>

<h2>自動功能（唔使出聲）</h2>
<table>
<tr><th>幾時</th><th>做乜嘢</th></tr>
{automation_rows}
</table>

<h3 class="timeline-title">股票分析自動化時間表</h3>
<p class="timeline-caption">六個獨立股票分析程序，由凌晨到朝早自動接力執行，資料寫落Notion</p>
<div class="timeline">
{stock_timeline}
</div>

<footer>私人系統：所有指令都要有授權金鑰先用得。呢頁由 asr/router.py 嘅指令表自動生成。</footer>
<script>
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


def _command_row(command_id: str) -> str:
    spec = COMMANDS[command_id]
    primary, *alts = spec["phrases"]
    desc = DESCRIPTIONS.get(command_id, "（未有說明）")
    if spec["destructive"]:
        desc += '<div class="confirm">⚠ 要講「確認」先執行</div>'
    return (
        "<tr>"
        f'<td class="phrase">{html.escape(primary)}</td>'
        f"<td>{desc}</td>"
        f'<td class="alt">{html.escape("、".join(alts))}</td>'
        "</tr>"
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
        chips.append(f'<button class="chip" data-cat="{html.escape(name)}">{html.escape(name)}</button>')
        rows = "\n".join(_command_row(cid) for cid in ids)
        groups.append(
            f'<section class="cmd-group" data-cat="{html.escape(name)}">\n'
            f"<h3>{html.escape(name)}</h3>\n"
            "<table>\n<tr><th>噉樣講</th><th>做乜嘢</th><th>其他講法</th></tr>\n"
            f"{rows}\n</table>\n</section>"
        )

    automation_rows = [
        f'<tr><td class="phrase">{html.escape(when)}</td><td>{html.escape(what)}</td></tr>'
        for when, what in AUTOMATIONS
    ]

    timeline_rows = [
        "<div class=\"t-row\">\n"
        f'<div class="t-time">{html.escape(time)}<br>{html.escape(period)}</div>\n'
        '<div class="t-rail"><span class="t-dot"></span><span class="t-line"></span></div>\n'
        '<div class="t-card">\n'
        '<div class="t-head">'
        f'<span class="t-cat">{html.escape(cat)}</span>'
        f'<span class="t-name">{html.escape(name)}</span>'
        "</div>\n"
        f'<p class="t-desc">{desc}</p>\n'
        "</div>\n</div>"
        for time, period, cat, name, desc in STOCK_TIMELINE
    ]

    return PAGE.format(
        filter_chips="\n".join(chips),
        command_groups="\n".join(groups),
        automation_rows="\n".join(automation_rows),
        stock_timeline="\n".join(timeline_rows),
        command_count=len(COMMANDS),
        automation_count=len(AUTOMATIONS),
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
