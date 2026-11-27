# Zeus

A proactive personal assistant you text over iMessage. Zeus watches your email, trips, orders, returns,
renewals and reminders, tells you what matters before you ask, and does the legwork. It is careful by
design: it never buys anything without your yes, keeps quiet hours, never redoes a send that may already
have happened, and only calls something done when it has proof.

Zeus is a plugin pack and engine for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## Install

```bash
pipx install zeus-ai          # or, before the PyPI release: pipx install git+https://github.com/davidson4302-crypto/zeus
zeus setup
zeus doctor
```

`zeus setup` walks you through everything below, checks what is already done, and is safe to run again.
Then text your new Zeus line "hi".

## Prerequisites

Zeus runs on three outside services. `zeus setup` checks for each one, offers to install what is missing,
and runs its sign-in for you.

| Prerequisite | What it does for Zeus | What setup does | Links |
|---|---|---|---|
| **Hermes Agent** | The agent framework Zeus runs inside: chat, tools, memory, scheduled jobs. Open source (MIT), by Nous Research. | Installs it if missing, creates a `zeus` profile, applies Zeus's safety patches, starts the gateway. | [GitHub](https://github.com/NousResearch/hermes-agent) · [Docs](https://hermes-agent.nousresearch.com/docs) |
| **Photon** | Gives Zeus its own iMessage number, so you text it like a person. Needs a Photon account. | Runs `hermes photon setup`: browser sign-in, project, your phone number, the iMessage line. | [Website](https://photon.codes) · [Docs](https://docs.photon.codes) |
| **Composio** | Lets Zeus read your Gmail (read-only) for orders, trips, returns and renewals. Needs a Composio account. | Installs the Composio CLI if missing, signs you in, links Gmail. | [Website](https://composio.dev) · [Docs](https://docs.composio.dev) |

You also need:

- **A Mac** (Zeus 1.0 is macOS only) with **Google Chrome** (store, flight and sign-in checks) and
  **Node.js + npm** (Photon's iMessage bridge; `brew install node`).
- **An AI model key**, for example an [Anthropic API key](https://console.anthropic.com). Setup asks for it.
- Optional: an [AgentMail](https://agentmail.to) key, so Zeus has its own inbox for receipts and sign-in codes.
- **pipx** to install Zeus: `brew install pipx`.

Tip: each new Photon project can move your phone's conversation to that project's line. Zeus uses one
project; avoid creating extra ones.

## What Zeus does

- **Life engine.** Every few minutes it reads what changed (mail, calendar, orders, flights) and decides
  what is worth a text, at the right moment, in plain words.
- **Hourly research.** One quiet research pass an hour, skipped when nothing is new.
- **Orders, returns and refunds** watched until they arrive or the money comes back.
- **Trips and flights**: gate changes, delays, when to leave.
- **Renewals and reservations**, with prepared next steps.
- **Phone sign-in handoff**: when a store signs Zeus out, it texts you a link to sign in from your phone.
- **Background helpers**: long jobs run in the background so chat stays quick.

## Safety

- **Purchase lock**: Zeus can press "Place order" only after your yes to that store and that total, once.
- **Job guard**: background jobs can look things up but cannot send, change or delete.
- **Quiet hours** (default 9pm to 9am on your clock): nothing reaches you unless you texted first.
- **Effect fence**: a send, booking or change that may already have happened is never redone on its own.
- **Proof before done**: a task is closed only with evidence.
- **Owner only**: only you can approve anything; other people and websites cannot speak for you.

### Known limits in 1.0

- Only your own iMessage number counts as you. Email from you, group chats and other apps never approve anything.
- A promised reply ("send it in 10 minutes") goes out once. If Zeus can't tell whether a send went through,
  it asks you instead of sending again.
- While a store or checkout page is open, Zeus won't run scripts or programs from the command line,
  so nothing can press "Place order" around the purchase lock. Zeus's own `zeus` tools still work.
- Photon status stickers are off.

## Configuration

Your details live in `~/.zeus/config.toml` (private to your Mac account). `zeus setup` writes it; edit it
any time and run `zeus doctor`. Common settings:

```toml
[owner]
name = "Alex Rivera"
first_name = "Alex"
phone = "+15555550100"
timezone = "America/New_York"

[owner.emails]
personal = "alex@example.test"

[quiet_hours]
start = 21
end = 9

[calendar]
snapshot_file = "/path/to/calendar.json"   # optional; calendar features are off without it

[mail.composio_accounts]
personal = "ca_..."                         # set by setup when you link Gmail
```

## Commands

| Command | What it does |
|---|---|
| `zeus setup` | Install or repair Zeus. `--dry-run` shows what it would do. |
| `zeus doctor` | Check every piece and say what to fix. |
| `zeus hermes-patch status` | Show whether Zeus's Hermes safety patches are applied. |
| `zeus life ...` | The life engine's command line. |
| `zeus uninstall` | Stop Zeus and remove its links. Keeps your data. |

## Privacy

Everything stays on your Mac except what the services above need to work: your model provider sees the
conversation, Photon carries the texts, Composio reads Gmail on Zeus's behalf. Zeus itself sends no telemetry.

## License

MIT. See [LICENSE](LICENSE).
