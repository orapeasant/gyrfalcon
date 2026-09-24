# Microsoft Teams gateway adapter

**Status:** Implemented with the Microsoft Teams Python SDK 2.x. The adapter,
setup wizard, documentation, and local protocol tests are present. A live tenant
test requires a registered bot and a public HTTPS endpoint.

## Transport and registration

Teams sends Bot Framework activities to `POST /api/messages` over public HTTPS.
Unlike Slack Socket Mode, there is no outbound-only transport. The gateway
starts the Teams SDK's FastAPI host on the configured address (default
`127.0.0.1:3978`); a tunnel or reverse proxy exposes it to Teams. The SDK
validates the incoming Bot Service JWT before the activity reaches the adapter.
The adapter additionally requires the configured tenant ID to match the
activity. Credentials are a single-tenant application/client ID and client
secret. The secret lives in the profile `.env` or secret store, never
`config.yaml`.

The SDK is an optional `[teams]` dependency. The gateway imports the adapter
only when `gateway.platforms.teams.enabled` is true.

## Message flow

1. The SDK accepts and authenticates the HTTP activity.
2. The Teams handler translates it into `MessageEvent` and schedules the
   gateway dispatch, then returns promptly to acknowledge the request.
3. The shared `GatewayRunner` deduplicates, checks user/channel allowlists and
   rate limits, handles commands and approvals, and runs the shared `AIAgent`.
4. The adapter sends or edits the reply using the Teams SDK. Channel replies
   use the root message ID; personal chats stay in one rolling session.

Teams sends channel messages to bots only when directly mentioned by default.
The adapter ignores unrelated channel messages even if the app has permission
to receive them. An unmentioned reply in a thread owned by the bot is accepted
if Teams delivers it (for example, with resource-specific consent).

`SessionSource` uses:

| Field | Teams value |
|---|---|
| `platform` | `teams` |
| `chat_id` | Base conversation ID, without `;messageid=` |
| `thread_id` | Channel thread root ID; empty for personal chat |
| `user_id` | Teams bot-scoped sender ID (`29:...`) |
| `guild_id` | Microsoft Entra tenant ID |
| `message_id` | Activity ID |

## Outbound delivery and limits

The delivery router accepts `teams:<conversation-id>`. The bot must already be
installed in that chat or team. Incoming activities supply the service URL for
responses; unsolicited sends use the SDK's public-cloud default if no URL has
been observed since gateway startup. Channel replies and streaming edits use
the observed service URL. The adapter splits long replies into small messages.

The `teams` toolset inherits `gateway_safe`, so chat users cannot call shell or
file-writing tools by default. The existing `!approve` / `!deny` text flow is
available. v1 does not read attachments, create chats, install the app, support
sovereign-cloud endpoints, or provide Teams Adaptive Card approval buttons.

## References

- [Teams SDK quickstart](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/getting-started/quickstart)
- [Register a Teams app](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/get-started/quickstart-register)
- [Channel threads and mentions](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-and-group-conversations)
- [Proactive messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
