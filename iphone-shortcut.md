# iPhone Shortcut — Voice Command Client

Build this in the **Shortcuts** app on the iPhone. Takes about 5 minutes. The result: tap a button (or say "Hey Siri, 語音指令"), speak Cantonese, and the phone speaks the result back.

## Before you start

You need the `VOICE_GATEWAY_KEY` value (from `gateway/.env` line 2 / Vercel env). It gets pasted into one header below.

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
  - **Headers** — add one:
    | Key | Value |
    |---|---|
    | `Authorization` | `Bearer <paste your VOICE_GATEWAY_KEY>` |
    (note the word `Bearer`, a space, then the key)
  - **Request Body**: `Form`
  - Add a form field:
    - **Key**: `file`
    - **Type**: `File`
    - **Value**: select the **Recorded Audio** variable (the output of step 1)

### 3. Get Dictionary Value

- Add the action **"Get Dictionary Value"**
- Configure it to get the value for key **`reply`** in **Contents of URL**

### 4. If (error branch — so failures are never silent)

- Add the action **"If"**
- Condition: **Dictionary Value** (from step 3) **has any value**
- Inside the **If** branch, add **"Speak Text"**:
  - Text: the **Dictionary Value** variable from step 3
  - Expand and set **Language** to Chinese (Hong Kong) so replies with 確認 are pronounced correctly
- Inside the **Otherwise** branch:
  - Add **"Get Dictionary Value"**: key **`error`** in **Contents of URL**
  - Add **"Speak Text"**: a text containing `錯誤 ` followed by that **Dictionary Value** variable
- The **End If** closes the shortcut

Now a successful command speaks the reply, and a failure speaks the server's reason (e.g. "multipart body must include a 'file' field").

### 5. Name and trigger

- Tap the shortcut name at the top → **Rename** → e.g. `語音指令`
- Ways to trigger it:
  - **Siri**: works immediately — "Hey Siri, 語音指令"
  - **Home screen**: shortcut settings → Add to Home Screen
  - **Action Button** (iPhone 15 Pro and later): Settings → Action Button → Shortcut → pick it
  - **Back Tap**: Settings → Accessibility → Touch → Back Tap → Double Tap → pick it

## Transcribe-only variant

Duplicate the shortcut, remove `?mode=command` from the URL, and change step 3 to read the key **`text`** instead of `reply`. That one just types back what you said — useful for dictation into notes.

## How to read the replies

| Spoken reply | Meaning |
|---|---|
| command reply (e.g. "voice OS online") | Command matched and executed |
| "say 確認 to run …" | Destructive command is pending — run the shortcut again and say 確認 (you have 60 seconds), or 取消 to cancel |
| an English answer to what you said | No command matched — the text went to the Ollama chat fallback (reply-only; chat can never trigger commands) |
| "chat engine unavailable" | No command matched and Ollama isn't running on the Win11 box |
| "nothing heard" | Silence or non-speech audio |

## Troubleshooting

| Symptom | Cause |
|---|---|
| "錯誤 multipart body must include a 'file' field" | Step 2 form field misconfigured — Key must be exactly `file`, **Type must be `File`** (Shortcuts defaults to Text!), Value must be the Recorded Audio variable |
| Spoken reply never comes, shortcut shows 401 | Wrong/missing Authorization header — check `Bearer ` prefix and key |
| 413 error | Recording too long — keep commands under ~2 minutes (16 MB cap) |
| 502 error | Win11 box unreachable — check the `Cloudflared` and `VoiceASR` services are running |
| Very slow first response after a reboot | Model loading into GPU on service start (~30 s), one-time per boot |

## Security notes

- The bearer key lives inside this shortcut. **Never share the shortcut via iCloud link** — that ships the key with it.
- If the phone is lost, rotate `VOICE_GATEWAY_KEY` on Vercel and in `gateway/.env`.
