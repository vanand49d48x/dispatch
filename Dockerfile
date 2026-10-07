FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

# Node 22, git, GitHub CLI, build tools
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg git jq build-essential ffmpeg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /usr/share/keyrings/githubcli.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/gh.list \
 && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
 && rm -rf /var/lib/apt/lists/*

# Vercel CLI, Claude Code CLI, Playwright + Chromium for QA
RUN npm install -g vercel @anthropic-ai/claude-code playwright @playwright/mcp \
 && npx playwright install --with-deps chromium \
 && PWV="$(node -p "require('$(npm root -g)/@playwright/mcp/package.json').dependencies.playwright")" \
 && npx -y "playwright@${PWV}" install chromium \
 && chmod -R a+rx /ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY harness ./harness

# Claude Code refuses some modes as root; run as an unprivileged user.
RUN useradd -m -u 1000 forge && mkdir -p /app/data && chown -R forge /app
USER forge
RUN git config --global init.defaultBranch main

CMD ["python", "-m", "harness", "--config", "/app/config.yaml"]
