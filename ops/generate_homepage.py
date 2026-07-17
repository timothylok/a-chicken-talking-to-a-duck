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
    "TODAY_AGENDA": "讀出今日Google日曆剩低嘅行程：時間同標題",
    "MORNING_BRIEFING": "一次過講晒：今日天氣、嚟緊嘅巴士、今日行程、收垃圾提醒（今日或聽日先講）同三條新聞",
    "QUOTE_OF_DAY": "隨機講一句周星馳電影金句",
    "MOVIE_QUOTE": "隨機講一句港產片對白，會講埋戲名同角色",
    "CREATE_REMINDER": "喺你部iPhone加提醒事項：講「提我」加內容同時間（例：提我聽日朝早九點買牛奶）",
    "RESTART_ASR": "重新啟動語音系統（約三十秒後恢復）",
    "TRIGGER_DEPLOY": "重新部署網站",
}

# Non-voice automations shown on the home page; the dashboard count derives
# from this list, so adding an automation = one entry here.
AUTOMATIONS = [
    ("朝早十點", "iPhone自動攞當日簡報然後讀出嚟：天氣、巴士、收垃圾提醒、新聞"),
    ("朝早九點", "檢查牛奶價錢，如果今日最平嘅3公升奶平過琴日，推送通知去手機"),
    ("每十分鐘", "系統心跳檢查 — 條通道或者語音服務死咗，手機即刻收到高優先通知"),
    ("每五分鐘", "指令紀錄自動同步去Notion（傾偈內容唔會離開屋企部機）"),
    ("朝早三點半", "自動清理舊紀錄：傾偈內容留30日，系統日誌留90日，指令紀錄長期保存"),
    ("每分鐘", "檢查提醒事項，到咗指定時間就推送通知去手機"),
    ("每十分鐘", "同步Google日曆去本機（淨係攞標題同時間，其他資料唔會落嚟）"),
    ("每分鐘", "執行自訂工作流規則：落雨提你帶遮、聽日收垃圾今晚提你、指令出錯即刻通知"),
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
  .flow {{ color: var(--muted); font-size: 0.9rem; overflow-x: auto; white-space: nowrap; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }}
  th {{ font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  .phrase {{ font-weight: 600; white-space: nowrap; }}
  .alt {{ color: var(--muted); font-size: 0.85rem; }}
  .confirm {{ color: var(--accent); font-size: 0.85rem; white-space: nowrap; }}
  .stats {{ display: flex; gap: 1rem; margin-top: 1.5rem; }}
  .stat {{ flex: 1; border: 1px solid var(--line); border-radius: 8px;
          padding: 0.75rem 1rem; text-align: center; }}
  .stat .num {{ font-size: 2rem; font-weight: 700; color: var(--accent); line-height: 1.2; }}
  .stat .label {{ color: var(--muted); font-size: 0.85rem; }}
  footer {{ margin-top: 3rem; color: var(--muted); font-size: 0.8rem;
           border-top: 1px solid var(--line); padding-top: 1rem; }}
</style>
</head>
<body>
<h1>雞同鴨講</h1>
<p class="tagline">私人廣東話語音OS</p>
<p class="translate"><a href="https://translate.google.com/translate?sl=auto&amp;tl=en&amp;u=https://a-chicken-talking-to-a-duck.vercel.app/" rel="nofollow">Translate to English (Google Translate)</a></p>

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
<table>
<tr><th>噉樣講</th><th>做乜嘢</th><th>其他講法</th></tr>
{rows}
</table>
<p>危險指令會先讀返你嘅指令出嚟，六十秒之內講「<strong>確認</strong>」先會執行，講「<strong>取消</strong>」就唔做。</p>
<p>講其他嘢？唔使指令，直接問 — 本地AI會用廣東話答你。</p>

<h2>自動功能（唔使出聲）</h2>
<table>
<tr><th>幾時</th><th>做乜嘢</th></tr>
{automation_rows}
</table>

<footer>私人系統：所有指令都要有授權金鑰先用得。呢頁由 asr/router.py 嘅指令表自動生成。</footer>
</body>
</html>
"""


def render() -> str:
    rows = []
    for command_id, spec in COMMANDS.items():
        primary, *alts = spec["phrases"]
        desc = DESCRIPTIONS.get(command_id, "（未有說明）")
        if spec["destructive"]:
            desc += '<div class="confirm">⚠ 要講「確認」先執行</div>'
        rows.append(
            "<tr>"
            f'<td class="phrase">{html.escape(primary)}</td>'
            f"<td>{desc}</td>"
            f'<td class="alt">{html.escape("、".join(alts))}</td>'
            "</tr>"
        )
    automation_rows = [
        f'<tr><td class="phrase">{html.escape(when)}</td><td>{html.escape(what)}</td></tr>'
        for when, what in AUTOMATIONS
    ]
    return PAGE.format(
        rows="\n".join(rows),
        automation_rows="\n".join(automation_rows),
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
