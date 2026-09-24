# Slack for Gyrfalcon

Talk to your Gyrfalcon agent from Slack — a DM, or an @mention in a channel.
Design and rationale: [`docs/spec/gyrfalcon/18-slack.md`](../spec/gyrfalcon/18-slack.md).

It uses **Socket Mode**: the gateway dials out to Slack, so you need no public
URL, no inbound firewall rule and no request-signature handling.

## Set up

1. **Install the SDK** (optional dependency, so a Slack-less install stays lean):
   ```bash
   uv sync --extra slack        # add --extra dev --extra postgres if you use those
   ```
   Pass every extra you want in one command: syncing without `--extra slack`
   removes it again, and the gateway then logs that the adapter could not be
   imported. The Slack tests skip themselves when it is missing, so a plain
   `uv run pytest` still passes.
2. **Create the app** at <https://api.slack.com/apps> → *Create New App* →
   *From a manifest*, and paste [`manifest.yaml`](manifest.yaml).
3. **Get two tokens.**
   - *Basic Information → App-Level Tokens → Generate*, with scope
     `connections:write`. Starts with `xapp-`.
   - *Install App → Install to Workspace*, then copy the *Bot User OAuth Token*.
     Starts with `xoxb-`.
4. **Find your member id**: your Slack profile → ⋮ → *Copy member ID* (`U01ABC…`).
5. **Configure.** Either run the wizard —

   ```bash
   gyrfalcon setup              # step 4 is Slack
   ```

   — or set it by hand. Tokens go in `~/.gyrfalcon/.env` (created owner-only),
   **never** in `config.yaml`, which the dashboard's config API returns:

   ```bash
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```
   ```yaml
   # ~/.gyrfalcon/config.yaml
   gateway:
     platforms:
       slack:
         enabled: true
         mode: socket
         toolset: slack               # the restricted set — see Security
         allow:
           users: ["U01ABCDEF"]       # REQUIRED. Empty means nobody.
           channels: ["C01XYZ123"]    # only for channel use; DMs need no entry
         respond_to: {dms: true, mentions: true, threads: true}
   ```
   The secret store also works: put the tokens there and name them with
   `bot_token_secret` / `app_token_secret` instead.
6. **Run it:** `gyrfalcon gateway`. It logs `Slack connected as … in workspace T…`.

## Using it

| You do | What happens |
|---|---|
| DM the bot | One rolling conversation per DM. Start a Slack thread inside the DM for a separate one. |
| `@Gyrfalcon …` in a channel | Answered **in a thread** on your message. That thread is the conversation. |
| Reply in that thread | No @mention needed. |
| Message while it is still working | Queued (👀), then handled together once the current reply finishes. |

The reply appears as soon as the agent starts writing and grows in place, so a
long answer is readable while it is still being written, and a running tool shows
as *…running `web_search`*. Progress also shows as a reaction on your message:
⏳ working → ✅ done / ❌ failed.

Turn it off, or slow it down, per platform:

```yaml
gateway:
  platforms:
    slack:
      stream: true              # false delivers the answer whole, once
      stream_interval_ms: 1500  # how often the message is updated (floor: 200)
```

### Getting messages you did not ask for

A scheduled job can post its result to Slack. Set the job's `deliver` field to a
destination — `slack:C0123` for a channel, `slack:@U0456` for a DM — and it
arrives when the job runs, titled with the job's name:

```yaml
deliver: "slack:C0123"     # the default, "local", just writes output to disk
```

The agent can also send to a destination itself with the `send_message` tool.
That tool is **not** in the restricted `slack` toolset: answering where it was
asked is one thing, posting into arbitrary channels is another, so it is opt-in.

Both need the gateway running — that is the only process holding a Slack
connection. A job triggered by hand from the CLI records
`last_delivery_error` instead of silently doing nothing.

Commands — prefix with `!` (Slack swallows a leading `/`): `!stop` interrupts the
current reply and clears the queue · `!reset` starts this thread over · `!status`
· `!approve` / `!deny` answer a pending approval · `!help`. In a channel:
`@Gyrfalcon !stop`.

A Slack thread is a conversation, and it is remembered: come back tomorrow, or
after a gateway restart, and it carries on.

## Security — read before adding anyone

Other people's text reaches the model here, so this is not the same trust
situation as your own terminal.

- **The allowlist is the only access control** while `identity.enabled` is off
  (the default). It fails closed: no `allow.users`, no answers. A channel message
  needs an allowed user *and* an allowed channel; adding the bot to a channel
  does not open it to that channel's members.
- **Dangerous tool calls stop and ask a person.** A command matching the
  dangerous-command list (`sudo`, `pip install`, `git push --force`, …) posts
  *Approve* / *Deny* buttons in the thread and does not run until somebody
  presses one. Only an allowlisted user's answer counts — a button in a channel
  can be clicked by anyone who can see it — and the thread records who decided.
  Unanswered questions expire after five minutes as a refusal. Commands on the
  hardline list (`rm -rf /`, fork bombs, disk writes) are refused outright and
  are never offered for approval.
- **The `slack` toolset is read-and-ask**: it excludes `terminal`, `execute_code`,
  `write_file`, `patch` and `skill_manage`. This is enforced when a tool is
  *called*, not just hidden from the model, and it holds through `delegate_task`
  and scheduled jobs (a chat agent cannot hand a child or a job more than it has).
  `read_file` and `search_files` refuse credential files (`~/.gyrfalcon/`, `.env`,
  `~/.ssh`, cloud credentials) — a best-effort denylist, not a sandbox.
- **Naming a toolset that includes a shell** (`toolset: core`) is allowed, but the
  gateway logs a warning at startup: it makes the allowlist the only thing between
  a Slack message and a shell on this machine.
- **Replies are scrubbed** of token-shaped strings and of the adapter's own tokens,
  and `<!channel>`, `<!here>` and `<@…>` in model output are neutralised so the
  agent cannot be talked into paging the workspace.
- Refusals are silent by default (`reply_on_deny: true` to say no out loud).
- Rate limit: `rate_limit_rpm` per user (default 30).

### Letting someone use the shell

Because every dangerous call now has to be approved by a person, the tools that
change the machine can be turned on for named users:

```yaml
gateway:
  platforms:
    slack:
      allow:
        users: ["U01ABCDEF", "U02GHIJKL"]
        elevated: ["U01ABCDEF"]     # may also use terminal, write_file, patch…
      elevated_toolset: core        # optional; `core` is the default for them
```

`elevated` is extra permission for somebody already in `users`, never a way in:
an id listed only under `elevated` gets nothing. Elevated turns are still capped
at dispatch — "wider" is not "unbounded" — and each dangerous call still stops
for approval. Leave `elevated` empty and nobody gets the shell, which is the
default.

## Not done yet

Reading attachments (the agent is told a file was shared but cannot open it),
Events-API/HTTP mode, and Slack slash commands. See the spec.

## Troubleshooting

| Log line | Cause |
|---|---|
| `adapter could not be imported (No module named 'slack_sdk')` | The extra is not installed — or `uv` pruned it. Re-run `uv sync --extra slack`. |
| `Slack: no bot token and app-level token` | Tokens not in `.env` / secret store. |
| `the bot token must start with 'xoxb-'` | Pasted the wrong token into the wrong slot. |
| `auth.test failed (invalid_auth)` | Token revoked, or the app is not installed to the workspace. |
| `Socket Mode connection failed` | App-level token lacks `connections:write`, or Socket Mode is off. |
| `Platform 'slack' has an empty allow.users list` | Nobody is allowed; add your member id. |
| Bot ignores a channel message | Channel id not in `allow.channels`, or you did not @mention it / reply in its thread. |
| No ⏳/✅ reactions | Missing `reactions:write` (logged once). Harmless — replies still work. |
| Reply arrives whole, not growing | `stream: false`, or a provider that does not stream. Harmless. |
| Scheduled job did not post | The gateway must be running. Check the job's `last_delivery_error`. |
| `Refused slack message from unauthorised user` | The allowlist doing its job; the log names the id to add. |
| Approval buttons never appear | Interactivity is off in the app settings, or the manifest predates it. |
| `Denied: nobody answered within 5 minutes` | The question expired. Ask again. |

## Verified without Slack, and not

Everything above is covered by tests that run a **fake Slack** (`tests/gateway/test_slack_protocol.py`): the real
`slack_sdk` client talks to a local server that speaks Socket Mode and the Web API, so the handshake,
acknowledgements, request encoding, automatic reconnect and full message round trips are exercised.

Only a real workspace can confirm: your workspace's app-approval settings, the exact scope set, Slack's
real event payloads for unusual message types, and rate-limit behaviour under load. A short live check:

1. `gyrfalcon gateway` shows `Slack connected as …`.
2. DM the bot "hello" → ⏳ appears, then a reply and ✅.
3. `@Gyrfalcon hello` in an allowed channel → reply lands in a thread; reply in it with no mention → answered.
4. Have someone *not* on the list DM it → no reply, and a `Refused …` line in `gyrfalcon logs`.
5. `!status`, then `!reset`.
