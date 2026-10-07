# Security

Dispatch runs AI agents that write and execute code with real credentials. Treat it like giving a contractor
access to your accounts.

**Blast radius**
- Agents run inside the Docker container. They see only this folder's `data/` and the read-only config mount -
  never the rest of your machine. Don't add extra volume mounts.
- Use scoped credentials: a GitHub classic token with only `repo`, `workflow`, `read:org` (no `delete_repo`),
  a dedicated Vercel team or account, a Supabase org used only for Dispatch, and **Stripe test keys**.
- Set a monthly spend limit in the Anthropic Console in addition to Dispatch's per-project budgets.

**Built-in guardrails**
- Destructive commands (deleting repos/projects/databases, buying domains, cloud deletes) need your "yes" in Discord.
  Add your own patterns with `extra_gated_patterns`.
- Production deploys are only allowed in the release step, after you reply **ship** (unless `autonomy: full`).
- Secrets are stripped from web searches and from the research library; keys pasted in Discord are saved to
  `data/secrets.env` (mode 600) and the message is deleted.
- Only `discord.owner_user_id` can command the bot.

**Never commit** `config.yaml` or `data/` (both are in `.gitignore`).

**Reporting a vulnerability:** please open a private security advisory on GitHub (Security → Advisories →
Report a vulnerability) rather than a public issue.
