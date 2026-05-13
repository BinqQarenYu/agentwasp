---
id: telegram
title: Telegram
description: Bridge architecture, input types, commands, live progress.
---

# Telegram

`agent-telegram` is a polling bridge that converts Telegram updates ↔ Redis Streams. No webhook, no domain, no SSL needed for the bridge itself.

## Architecture

```
Telegram API ←─┐
               │  long-polling
               ▼
       agent-telegram (Python)
               │
               │ XADD events:incoming
               ▼
       agent-redis (Streams)
               │
               │ XREADGROUP
               ▼
       agent-core (handlers)
               │
               │ XADD events:outgoing
               ▼
       agent-redis (Streams)
               │
               │ XREADGROUP
               ▼
       agent-telegram (Python)
               │
               │ Telegram API (sendMessage / editMessage)
               ▼
        User receives reply
```

## Configuration

```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_ALLOWED_USERS=12345678         # comma-separated numeric user IDs
SCHEDULER_NOTIFY_CHAT_ID=12345678       # where the agent sends proactive alerts
```

**Fail-closed.** If `TELEGRAM_BOT_TOKEN` is set but `TELEGRAM_ALLOWED_USERS` is empty, the bridge refuses to start with an error message — there is **no public-bot mode and no escape hatch**. The onboarding wizard enforces this by requiring a numeric Telegram ID (5–15 digits) and replicating it to both env vars.

Only Telegram user IDs in the allowlist can interact. Other senders receive an "Unauthorized" reply.

## Input types

| Input | Handling |
|-------|----------|
| Text | Full pipeline |
| Photo | Vision-capable model receives the image bytes |
| Document | Treated as file; can be referenced in subsequent messages |
| Voice note | Transcribed via OpenAI Whisper, then passed through the text pipeline |
| Video / video note | First frame extracted with `ffmpeg` (added to the agent-core Dockerfile), then vision pipeline |

The bridge writes uploads to `/data/shared/uploads/<uuid>.{jpg,mp4,...}` so handlers can reference them.

## Built-in commands

| Command | Effect |
|---------|--------|
| `/start` | Concise welcome with capability summary (multi-language: EN/ES/PT/FR) |
| `/help` | Full command reference |
| `/ping` | Quick reachability check |
| `/status` | System status |
| `/memory` | Memory subsystem info |
| `/snapshot` | Save current state to a memory snapshot |
| `/model` | Show active model + provider |
| `/skills` | List registered skills |
| `/skill <name> <args>` | Invoke a skill directly |
| `/schedule` | Show scheduled tasks |
| `/introspect` | Capability + health snapshot |
| `/monitor <url>` | Watch a URL for changes |
| `/broker` | Integrations management |
| `/api set <provider> <key>` | Persist a model API key |
| `/openclaw <action>` | Manage dynamic skills from the ClawHub registry |

## Live progress

While the agent is working, it publishes `TELEGRAM_PROGRESS` events. The bridge edits a single status message in place:

```
🔧 Working...
└─ Step 1: web_search
└─ Step 2: render_report
```

Only one progress message per turn. On edit failure (Telegram rate limit or message-too-old), the bridge silently drops the update — it never sends a fallback message.

When the final response arrives, the bridge edits the status message into the final content, or replaces it with the final message and deletes the status.

## Boot sequence

The first message after a fresh start triggers a special boot sequence (`_run_boot_sequence`):

```
🚀 WASP starting up...
✓ Telegram connectivity
✓ Model live (anthropic claude-sonnet-4-6)
✓ Knowledge graph ready
✓ Browser session ready
✓ Memory subsystem ready
```

If any check fails, the corresponding line shows ✗ with a hint. When the boot sequence runs after a Panic Reset, an additional message warns that all cognitive memory has been cleared.

## Output sanitization

Before sending, every response passes through `_clean_telegram_output()`:

- Strips markdown image syntax `![...](...)` (file paths must not appear in user output)
- Collapses `[text](url)` to `text (url)`
- Strips bold/italic/inline-code markers
- Strips header `#` prefixes
- Removes horizontal rules
- Strips internal paths and prompt-leak fragments
- Strips system prefixes like `[TAREA PROGRAMADA:]`, `EJECUTA AHORA`, etc.

The result is plain text that renders cleanly in Telegram without raw markdown leaking.

## Voice notes

Voice notes are downloaded by the bridge to `/data/shared/uploads/voice_<uuid>.ogg`. The handler invokes OpenAI Whisper API for transcription (requires `OPENAI_API_KEY`). The transcribed text replaces the original audio and goes through the full pipeline.

## Video / video notes

`filters.VIDEO | filters.VIDEO_NOTE` handler in the bridge downloads videos to `/data/shared/uploads/video_<uuid>.mp4`. The handler uses `ffmpeg` to extract the first frame:

```bash
ffmpeg -i video.mp4 -vf "select=eq(n\,0)" -frames:v 1 -q:v 2 frame.jpg
```

The frame is then treated as an image (vision pipeline).

## Bridge restart safety

On bridge restart, Redis Streams + consumer groups guarantee at-least-once delivery. PEL zombie recovery (`xautoclaim` at startup, idle > 5 min) recovers messages from a previously-crashed consumer.

## See also

- [Operator Commands](/operations/commands) — daily usage
- [Dashboard](/integrations/dashboard) — alternative interface
- [Logs](/operations/logs) — log surfaces
