# Hermes + Telethon for Hugging Face Spaces
# Base: published NousResearch Hermes agent image
FROM nousresearch/hermes-agent:latest

# --- Install Telethon + SOCKS proxy support (needed for mtproto from sandboxes) ---
RUN python -m pip install --no-cache-dir telethon==1.45.0 python-socks==3.2.0

# --- Copy the Telethon platform plugin (from your hermes-agent-telethon fork) ---
# Build context must include plugins/platforms/telethon/
COPY plugins/platforms/telethon/ /opt/hermes/plugins/platforms/telethon/

# --- Telethon config defaults (override in HF Spaces Secrets/Variables) ---
ENV TELETHON_API_ID=""
ENV TELETHON_API_HASH=""
ENV TELETHON_BOT_TOKEN=""
ENV TELETHON_ALLOWED_USERS=""
ENV TELETHON_HOME_CHANNEL=""
ENV TELETHON_PROXY=""

# --- Hermes runtime ---
ENV HERMES_PROFILE=default

# Keep container alive: run the gateway as PID 1
CMD ["hermes", "gateway", "run"]
