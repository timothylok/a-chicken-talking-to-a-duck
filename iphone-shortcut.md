# iPhone Shortcut — Voice Command Client

Build this in the **Shortcuts** app on the iPhone. Takes about 5 minutes. The result: tap a button (or say "Hey Siri, 語音指令"), speak Cantonese, and the phone speaks the result back.

## Before you start

You need the `VOICE_GATEWAY_KEY` value (from `gateway/.env` line 2 / Vercel env). It gets pasted into one header below.

## How the shortcut flows

One shortcut handles every kind of response. The server always returns JSON with these fields:

| Field | When present | What the shortcut does with it |
|---|---|---|
| `reply` | every successful request — commands, chat fallback, confirmations, reminders | speak it |
| `reminder` | only when you said 提我／提醒我／remind me and it parsed | create an iOS Reminder locally (the `title` includes the due time), then still speak `reply` |
| `error` | request failed (auth, upload, server unreachable) | speak it so failures are never silent |

```
Record Audio → POST to gateway
   → reminder present?  → yes: create iOS Reminder (silent)
   → speak reply — or speak error if there is no reply
```

The complete action list — two sequential If blocks, **no nesting anywhere**, and **no alert on the reminder**: iOS 2026's "Add New Reminder" action rejects every dynamic alert value (tested exhaustively: date variable, formatted text, time-only — all "alert time provided was invalid"; only static picker values work). Instead the server puts the due time in the reminder's title, e.g. 「買牛奶（7月15號朝早9點）」.

```
 1. Record Audio
 2. Get Contents of URL                       (POST, form field "file")
 3. Get Dictionary Value  reminder            ← Contents of URL
 4. If [Dictionary Value] has any value
 5.     Get Dictionary Value  title           ← reminder value from 3
 6.     Add New Reminder  title               (Alert: None)
 7. End If
 8. Get Dictionary Value  reply               ← Contents of URL
 9. If [reply] has any value
10.     Speak Text  reply
11. Otherwise
12.     Get Dictionary Value  error           ← Contents of URL
13.     Speak Text  「錯誤」 + error
14. End If
```

Non-reminder commands sail straight through: `reminder` is absent, the If at line 4 does nothing, and the reply is spoken at line 10. Reminder commands do both — create the reminder, then speak the confirmation. A reminder spoken without a time isn't created — the server replies asking you to say it again with one (the time goes into the title).

## Steps

Open Shortcuts → **+** (new shortcut), then add these actions in order:

### 1. Record Audio

- Search for the action **"Record Audio"** and add it
- Tap the arrow on the action to expand options:
  - **Audio Quality**: Normal (smaller upload = faster; quality is plenty for ASR)
  - **Start Recording**: On Tap (or "Immediately" if you want zero taps after launch)
  - **Finish Recording**: On Tap

### 2. Get Contents of URL

- Add the action **"Get Contents of URL"**
- Set the URL to:
  ```
  https://a-chicken-talking-to-a-duck.vercel.app/api/voice?mode=command
  ```
- Expand the action (tap the arrow) and configure:
  - **Method**: `POST`
  - **Headers** — add two:
    | Key | Value |
    |---|---|
    | `Authorization` | `Bearer <paste your VOICE_GATEWAY_KEY>` |
    | `X-Timestamp` | the **Current Date** magic variable |
    (note the word `Bearer`, a space, then the key)
  - For `X-Timestamp`: tap the value field, pick **Current Date** from the variable bar, then tap the inserted variable → **Date Format: ISO 8601** → turn **ISO 8601 Time** on. The gateway rejects requests without a timestamp within 5 minutes (replay protection), so this header is required.
  - **Request Body**: `Form`
  - Add a form field:
    - **Key**: `file`
    - **Type**: `File`
    - **Value**: select the **Recorded Audio** variable (the output of step 1)

### 3. Reminder branch (lines 3–7 of the action list)

Commands starting with 提我／提醒我／remind me return a `reminder` object; this branch turns it into a real iOS Reminder. The server never touches your Reminders — the phone creates it locally. Non-reminder responses have no `reminder` key, so this whole branch is skipped and execution falls through to step 4.

- Add **"Get Dictionary Value"**: key **`reminder`** in **Contents of URL**
- Add **"If"**: condition — that **Dictionary Value** **has any value**
- Inside the **If** branch (drag these two actions between **If** and **End If**):
  - **"Get Dictionary Value"**: key **`title`** in the **reminder** Dictionary Value
  - **"Add New Reminder"**: the **title** variable, **Alert: None** — do not set an alert; dynamic alert values are broken in Shortcuts (see the note above the action list). The due time is already in the title text
- **End If**
- First run: iOS will ask for permission to access Reminders — allow it
- Type every key (`reminder`, `title`) with the English keyboard and no trailing spaces

### 4. Speak the result (lines 8–14 — every flow ends here)

This is where **all** responses — commands, chat, confirmations, reminders — get spoken. Failures speak the server's reason instead, so they are never silent.

- Add **"Get Dictionary Value"**: key **`reply`** in **Contents of URL**
  - ⚠️ Make sure the dictionary is **Contents of URL**, not the `reminder` value from step 3 — after the If block, Shortcuts may auto-suggest the wrong variable
- Add **"If"**: condition — that **Dictionary Value** **has any value**
- Inside the **If** branch, add **"Speak Text"**:
  - Text: the **reply** Dictionary Value variable
  - Expand and set **Language** to Chinese (Hong Kong) so replies with 確認 are pronounced correctly
- Inside the **Otherwise** branch:
  - Add **"Get Dictionary Value"**: key **`error`** in **Contents of URL**
  - Add **"Speak Text"**: a text containing `錯誤 ` followed by that **Dictionary Value** variable
- The **End If** closes the shortcut

### 5. Name and trigger

- Tap the shortcut name at the top → **Rename** → e.g. `語音指令`
- Ways to trigger it:
  - **Siri**: works immediately — "Hey Siri, 語音指令"
  - **Home screen**: shortcut settings → Add to Home Screen
  - **Action Button** (iPhone 15 Pro and later): Settings → Action Button → Shortcut → pick it
  - **Back Tap**: Settings → Accessibility → Touch → Back Tap → Double Tap → pick it

## Morning briefing automation (spoken, no tap)

A second, tiny shortcut plus a personal automation: every morning the phone fetches the briefing as **text** (no recording) and speaks it. The server treats `{"text": "早晨"}` exactly like hearing you say 早晨 — it logs to history and syncs to Notion like a spoken command.

### The shortcut

Create a new shortcut named e.g. `早晨簡報`, with these actions:

```
1. Get Contents of URL                        (POST, JSON body)
2. Get Dictionary Value  reply                ← Contents of URL
3. If [reply] has any value
4.     Speak Text  reply
5. Otherwise
6.     Get Dictionary Value  error            ← Contents of URL
7.     Speak Text  「錯誤」 + error
8. End If
```

- **Get Contents of URL** — same URL and headers as the main shortcut (step 2 above): `?mode=command`, `Authorization`, `X-Timestamp` (Current Date, ISO 8601 with time). The only difference is the body:
  - **Request Body**: `JSON`
  - Add a field: **Key** `text`, **Type** `Text`, **Value** `早晨`
- Steps 2–8 are identical to step 4 of the main shortcut (no reminder branch needed — 早晨 never returns one). Set **Speak Text** Language to Chinese (Hong Kong).
- Test it by running the shortcut manually once — it should speak the full briefing.

### The automation

Shortcuts app → **Automation** tab → **+**:

- **Trigger**: either
  - **Time of Day** → pick e.g. 7:00, Daily (simplest, survives Focus schedule changes), or
  - **When "Sleep" Focus turns off** — fires the moment your morning Focus/DND ends
- **Run**: select the `早晨簡報` shortcut
- Set **Run Immediately** (not "Ask Before Running") so it speaks without confirmation

Notes:
- Speak Text plays through the speaker even with the silent switch on (it's media audio); volume follows the media volume, not the ringer.
- The briefing takes a few seconds to generate (weather + buses + news translation) — the pause before speech is normal.
- Running it twice within a minute returns "duplicate request ignored" — identical text bodies inside 60 s are treated as a network retry.

## Transcribe-only variant

Duplicate the shortcut, remove `?mode=command` from the URL, delete the reminder branch (step 3), and change step 4 to read the key **`text`** instead of `reply`. That one just types back what you said — useful for dictation into notes.

## How to read the replies

| Spoken reply | Meaning |
|---|---|
| command reply (e.g. "voice OS online") | Command matched and executed |
| "say 確認 to run …" | Destructive command is pending — run the shortcut again and say 確認 (you have 60 seconds), or 取消 to cancel |
| "好，聽日朝早9點提你買牛奶" | Reminder created in the Reminders app (the spoken time/title echoes exactly what was parsed — if it sounds wrong, delete it on the phone) |
| "唔明你想提咩…" | Reminder phrasing couldn't be parsed — say it again with the task and time |
| "要講埋幾點提你…" | Reminder understood but no time given — a time is required; say it again with one |
| an English answer to what you said | No command matched — the text went to the Ollama chat fallback (reply-only; chat can never trigger commands) |
| "chat engine unavailable" | No command matched and Ollama isn't running on the Win11 box |
| "nothing heard" | Silence or non-speech audio |

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every run lands in the error branch even though the server is fine | Invisible trailing space in a dictionary key (iOS auto-inserts one after typed words) — delete the key text in "Get Dictionary Value" and retype it with the English keyboard: `reply`, `error`, `reminder`, `title` |
| "錯誤 multipart body must include a 'file' field" | Step 2 form field misconfigured — Key must be exactly `file`, **Type must be `File`** (Shortcuts defaults to Text!), Value must be the Recorded Audio variable |
| Spoken reply never comes, shortcut shows 401 | Wrong/missing Authorization header — check `Bearer ` prefix and key |
| "錯誤 missing or stale X-Timestamp header…" | `X-Timestamp` header missing or not ISO 8601 with time (see step 2), or the phone's clock is more than 5 minutes off |
| "錯誤 duplicate request ignored" | The same request arrived twice within a minute (network retry) — the first one already ran; just run the shortcut again if you wanted a new command |
| 413 error | Recording too long — keep commands under ~2 minutes (16 MB cap) |
| 502 error | Win11 box unreachable — check the `Cloudflared` and `VoiceASR` services are running |
| Very slow first response after a reboot | Model loading into GPU on service start (~30 s), one-time per boot |
| Reminder speaks OK but nothing appears in Reminders | Shortcut's Reminders permission denied — Settings → Privacy → Reminders → Shortcuts, or re-run and allow |
| "The alert time provided was invalid… ensure you provide a time of day" | An alert is set on "Add New Reminder" — set **Alert: None**. Shortcuts rejects every dynamic alert value on this iOS version; the due time lives in the title instead |

## Security notes

- The bearer key lives inside this shortcut. **Never share the shortcut via iCloud link** — that ships the key with it.
- If the phone is lost, rotate `VOICE_GATEWAY_KEY` — full procedure in `CLAUDE.md` under "Rotating `VOICE_GATEWAY_KEY`".
