# Hermes Agent + Telethon on Hugging Face Spaces
FROM nousresearch/hermes-agent:latest

# Install Telethon + SOCKS proxy (mtproto needs it from sandboxes)
RUN /opt/hermes/.venv/bin/python -m pip install --no-cache-dir telethon==1.45.0 python-socks==3.2.0

# Add the Telethon platform plugin from your GitHub fork
RUN mkdir -p /opt/hermes/plugins/platforms/telethon && \
    git clone --depth 1 https://github.com/taslimx19/hermes-agent-telethon.git /tmp/ht && \
    cp -r /tmp/ht/plugins/platforms/telethon/* /opt/hermes/plugins/platforms/telethon/ && \
    rm -rf /tmp/ht

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
