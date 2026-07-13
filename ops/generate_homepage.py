"""Generate gateway/public/index.html from the command table in asr/router.py.

Run by the pre-commit hook (ops/githooks/pre-commit) whenever asr/router.py
is committed, so the public home page always matches COMMANDS. Output is
deterministic: same COMMANDS -> byte-identical HTML.

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
    "FUEL_PRICES": "報告附近最平嘅95汽油油站同價錢，最平排最先",
    "BUS_TIMES": "報告Glenfield Mall嚟緊嘅三班巴士：路線同幾多分鐘後開",
    "TIDE_TIMES": "報告奧克蘭下次潮漲潮退嘅時間同水位",
    "RESTART_ASR": "重新啟動語音系統（約三十秒後恢復）",
    "TRIGGER_DEPLOY": "重新部署網站",
}

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
  .flow {{ color: var(--muted); font-size: 0.9rem; overflow-x: auto; white-space: nowrap; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--line);
           vertical-align: top; }}
  th {{ font-size: 0.85rem; color: var(--muted); font-weight: 600; }}
  .phrase {{ font-weight: 600; white-space: nowrap; }}
  .alt {{ color: var(--muted); font-size: 0.85rem; }}
  .confirm {{ color: var(--accent); font-size: 0.85rem; white-space: nowrap; }}
  footer {{ margin-top: 3rem; color: var(--muted); font-size: 0.8rem;
           border-top: 1px solid var(--line); padding-top: 1rem; }}
</style>
</head>
<body>
<h1>雞同鴨講</h1>
<p class="tagline">私人廣東話語音OS</p>

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
    return PAGE.format(rows="\n".join(rows))


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(render())
    print(f"wrote {os.path.relpath(OUT_PATH, ROOT)} ({len(COMMANDS)} commands)")
