# Launching Teachloop on Dispatch

## 1. Keys (optional before start; mocks run until they're added)

Add any you have under `extra_env:` in `config.yaml`. They're picked up on the next agent run, no restart.
Or paste `NAME=value` lines in the project's Discord thread (the bot saves them and deletes your message).

```yaml
extra_env:
  # LLM for the product itself (can be the same key as anthropic.api_key)
  APP_ANTHROPIC_API_KEY: ""      # optional separate key for the app; blank = reuse your harness key
  # Payments — use TEST keys (sk_test_..., Connect enabled in the Stripe dashboard)
  STRIPE_SECRET_KEY: ""
  STRIPE_PUBLISHABLE_KEY: ""
  # Email
  RESEND_API_KEY: ""
  # Google login
  GOOGLE_CLIENT_ID: ""
  GOOGLE_CLIENT_SECRET: ""
  # Jobs (Inngest Cloud; without these, steps run inline)
  INNGEST_EVENT_KEY: ""
  INNGEST_SIGNING_KEY: ""
  # Storage (Cloudflare R2)
  R2_ACCOUNT_ID: ""
  R2_ACCESS_KEY_ID: ""
  R2_SECRET_ACCESS_KEY: ""
  R2_BUCKET: ""
  # Video + transcription (needed from milestone 7)
  MUX_TOKEN_ID: ""
  MUX_TOKEN_SECRET: ""
  DEEPGRAM_API_KEY: ""
```

Fastest path to a sellable demo: **Stripe test keys + Resend** (milestone 4 onward). Everything else can stay mocked.

## 2. Start it

In your `#dispatch` channel, attach `SPEC.md` and send:

> Build Teachloop from the attached spec. Milestone by milestone, deploy each to preview, production after QA.

Or: `/build idea:Teachloop spec:<attach SPEC.md> budget:150`

Terminal instead of Discord: `python -m harness --cli "Teachloop" --spec examples/teachloop/SPEC.md --budget 150`

## 3. What happens

1. Architect reads the spec → posts 8 milestones + the milestone 1 task list + the key list → you reply **go**.
2. Team builds milestone 1, deploys preview, QA tests it, releases to production → report in the thread.
3. Retro adds lessons to `data/playbook.md` (used by every future build).
4. Reply **next** (or `/next`) for milestone 2. Set `auto_continue_milestones: true` to run all 8 unattended.

Check cost after milestone 1 and adjust the cap with `/budget`.
