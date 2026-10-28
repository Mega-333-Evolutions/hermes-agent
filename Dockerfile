# Hermes Agent + Telethon on Hugging Face Spaces
FROM nousresearch/hermes-agent:latest

# --- Build arg: GitHub PAT to clone your private Telethon plugin repo ---
ARG GITHUB_TOKEN

# Install uv (Hermes venv has no pip)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Telethon + SOCKS proxy (mtproto needs it from sandboxes)
RUN uv pip install --no-cache --python /opt/hermes/.venv/bin/python \
    telethon==1.45.0 python-socks

# Add the Telethon platform plugin (copied from build context)
COPY plugins/platforms/telethon/ /opt/hermes/plugins/platforms/telethon/

# Telethon config (override via HF Space Secrets — do NOT hardcode real tokens)
ENV TELETHON_API_ID=""
ENV TELETHON_API_HASH=""
ENV TELETHON_BOT_TOKEN=""
ENV TELETHON_ALLOWED_USERS=""
ENV TELETHON_HOME_CHANNEL=""
ENV TELETHON_PROXY=""

ENV HERMES_PROFILE=default

# Start Hermes gateway as PID 1
CMD ["hermes", "gateway", "run"]
