# Microsoft Teams for Gyrfalcon

The Teams adapter lets an installed bot answer personal chats and channel
mentions through the shared Gyrfalcon gateway. It uses the current Microsoft
Teams Python SDK and requires a **public HTTPS** messaging endpoint.

## Set up

1. Install the optional dependency from this checkout:

   ```bash
   uv sync --extra teams
   ```

   Include any other extras you use in the same `uv sync` command, such as
   `--extra slack`.

2. Expose local port `3978` at a public HTTPS address with a tunnel or reverse
   proxy. Teams must be able to POST to
   `https://<your-host>/api/messages`. The gateway listens on
   `127.0.0.1:3978` by default. For a reverse proxy on another machine, set
   `gateway.platforms.teams.host: 0.0.0.0` and protect the host at your network
   boundary; the SDK still validates Bot Service JWTs.

3. [Register the app and bot with the Teams Developer CLI](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/get-started/quickstart-register),
   using your public endpoint:

   ```bash
   npm install -g @microsoft/teams.cli
   teams login
   teams status
   teams app create --name Gyrfalcon --endpoint https://<your-host>/api/messages --env /tmp/gyrfalcon-teams.env
   ```

   The CLI gives you an install link and writes `CLIENT_ID`, `CLIENT_SECRET`,
   and `TENANT_ID` to the temporary file. Your tenant must allow custom app
   upload. Install the app in Teams before messaging it.

4. Run `uv run gyrfalcon setup` and choose **Microsoft Teams** in step 5. Enter
   the client ID, tenant ID, client secret, and allowed user IDs. The wizard
   stores the secret in the active profile's `.env` and the other settings in
   `config.yaml`. You can also configure it manually:

   ```dotenv
   # ~/.gyrfalcon/.env (or $GYRFALCON_HOME/.env)
   TEAMS_CLIENT_SECRET=<client-secret-value>
   ```

   ```yaml
   # ~/.gyrfalcon/config.yaml (or $GYRFALCON_HOME/config.yaml)
   gateway:
     platforms:
       teams:
         enabled: true
         client_id: <application-client-id>
         tenant_id: <directory-tenant-id>
         host: 127.0.0.1
         port: 3978
         toolset: teams
         allow:
           users: ["29:your-bot-scoped-user-id"]
           channels: ["19:allowed-channel-id@thread.tacv2"]
   ```

   `TEAMS_CLIENT_ID` and `TEAMS_TENANT_ID` in the profile `.env` also work in
   place of their config keys. A `client_secret_secret` config key can name a
   stored secret instead of `TEAMS_CLIENT_SECRET`.

5. Start `uv run gyrfalcon gateway`. With
   `GYRFALCON_LOG_LEVEL=INFO`, `gateway.log` reports
   `Teams listening on 127.0.0.1:3978/api/messages`. This means the local
   endpoint is running; send a personal chat to check the public route and bot
   registration.

## Allow users and channels

The allowlists fail closed. For a first connection, you can leave both lists
empty, send the bot a personal chat or mention it in a channel, and read the
`Refused teams message from unauthorised user=... chat=...` line in
`gateway.log`. Copy the exact `user` value into `allow.users`; for channels,
copy the exact base `chat` value into `allow.channels`. Restart the gateway
after editing `config.yaml`.

A personal chat needs an allowed user. A channel needs an allowed user **and**
channel. Mention the bot in a new channel message; it replies in that thread.
Teams normally sends only mentioned messages to bots in channels, so mention
the bot again on follow-ups unless the app has permission to receive all
thread messages.

## Scheduled delivery and commands

Set a scheduled job's `deliver` target to `teams:<conversation-id>` to post
without a preceding user message. The bot must already be installed there,
and the gateway must be running. `!stop`, `!reset`, `!status`, `!approve`, and
`!deny` use the same gateway controls as Slack. Streaming replies are enabled
by default; set `stream: false` under `gateway.platforms.teams` to send a
single final answer.

## Troubleshooting

Read `gateway.log` with `uv run gyrfalcon logs --file gateway.log --follow`.
The endpoint itself returns HTTP 401 to unsigned requests; that is expected.
If Teams cannot reach it, verify the public HTTPS URL and tunnel. If the
listener starts but sends fail, check the client ID, client secret value,
tenant ID, and bot installation. A `Refused teams message` line means the
allowlist is blocking it.

The local tests exercise activity mapping, channel threads, message updates,
HTTP startup and shutdown, and rejection of unsigned requests. A live tenant
check still requires the app registration and public endpoint above.
