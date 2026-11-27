# Zeus skills

Custom Hermes skills that ship with Zeus. Install them into the Zeus profile's `skills/` folder keeping the `<category>/<name>/` layout. Scripts find the engine settings through their own location inside this package, or through `ZEUS_ENGINE_DIR`; skills copied elsewhere should set that variable (or `HERMES_HOME`).

- `zeus-renewals` (productivity): tracks a subscription renewal from the provider's notice: last day to cancel, next charge, the owner's keep/cancel decision and the payment receipt.
- `zeus-reservations` (productivity): prepares, books and verifies a hotel room (Trip.com) or restaurant table (OpenTable) through the purchase lock and an exact owner yes.
- `zeus-itinerary` (productivity): saves a source-backed trip and exports a written itinerary plus an importable .ics file.
- `zeus-calendar` (productivity): answers calendar questions from the configured calendar snapshot or a live read; never changes a calendar without the owner's yes.
- `calendar-schedule-lookup` (productivity): read-only, coverage-checked schedule lookup across Google Calendar (Composio or gog) and Apple Calendar.
- `scheduled-checks-and-followups` (productivity): schedules timed checks, reminders and flight or pickup watches, and proves them with a readback.
- `zeus-work-follow-through` (productivity): how to do proactive work, own handoffs, recover from stalls and report honestly.
- `zeus-mail-and-reports` (productivity): reply and report style, link rules, and Zeus's own mailbox tools.
- `html-to-pdf-with-visual-qa` (productivity): builds a designed PDF from HTML and checks every page visually before delivery.
- `restaurant-order-price-check` (productivity): read-only pickup price lookup on a restaurant chain's order site; never orders.
- `microsoft-office` (productivity): create, read and edit Word, PowerPoint and Excel files offline (MIT, Nous Research).
- `outbound-messages-and-files` (communication): sends a file or message to someone safely: exact file, full address, one yes, one send, readback.
- `agent-owned-mail` (email): reads and replies from the agent's own AgentMail inbox.
- `company-records` (company): records goals, facts and promised checks only as far as a readback proves them.
- `background-watcher-autostart` (devops): makes a background watcher wake the agent and restart with the gateway.
- `chat-reply-latency-check` (devops): measures real reply times and connection drops from the gateway logs.
- `local-terminal-session-inspection` (devops): lists open terminal and AI CLI sessions by topic and closes them only with a yes.
- `mcp-server-connections` (autonomous-ai-agents): connects and verifies MCP servers, including Composio app connections.
- `shared-link-review` (research): reads a shared link or post in full and gives a grounded opinion.
