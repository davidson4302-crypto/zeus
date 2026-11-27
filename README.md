# Zeus

A proactive personal assistant you text over iMessage. Zeus watches your email,
calendar, trips, orders and reminders, tells you what matters before you ask,
and does the legwork. It is careful by design: it never buys anything without
your yes, keeps quiet hours, never redoes a send that may already have happened,
and only calls something done when it has proof.

Zeus is a plugin pack and engine for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

> Status: being prepared for its first public release. Not installable yet.

## What you need first

Zeus runs on top of two outside projects. You set up both before installing Zeus.

| Dependency | What it does for Zeus | Link |
|---|---|---|
| **Hermes Agent** (required) | The agent framework Zeus runs inside: chat, tools, memory, scheduled jobs. Open source, MIT, by Nous Research. | [GitHub](https://github.com/NousResearch/hermes-agent) · [Docs](https://hermes-agent.nousresearch.com/docs) |
| **Photon** (required) | Gives Zeus its own iMessage phone number, so you text it like a person. Needs a Photon account. | [Website](https://photon.codes) · [Docs](https://docs.photon.codes) |
| An AI model key (required) | The model Zeus thinks with, for example an [Anthropic API key](https://console.anthropic.com). | |
| **AgentMail** (optional) | Zeus's own email inbox, for receipts and sign-in codes. | [Website](https://agentmail.to) |
| A Mac with Google Chrome | Zeus 1.0 is Mac only (iMessage, Reminders, Chrome for store and flight checks). | |

### 1. Install Hermes Agent

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

Then run `hermes` once and finish its first-time setup. Details: [Hermes docs](https://hermes-agent.nousresearch.com/docs).

### 2. Set up Photon

1. Create an account at [photon.codes](https://photon.codes).
2. Create one project with the iMessage platform. That gives you a line Zeus texts from.
3. Install the Photon command line tool: `bun add -g @photon-ai/cli`
   (use bun; a global npm install of this tool is known to crash).
4. Keep the project's credentials handy; `zeus setup` asks for them.

Tip: each new Photon project can take over your phone's conversation with your
other Photon lines. Create the one project Zeus needs and no more.

### 3. Install Zeus

```bash
pipx install zeus-ai     # coming soon
zeus setup               # asks for your name, phone, quiet hours and keys
zeus doctor              # checks every connection and safety switch
```

## License

MIT. See [LICENSE](LICENSE).
