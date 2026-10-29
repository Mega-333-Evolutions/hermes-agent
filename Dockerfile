FROM nousresearch/hermes-agent:latest

# Install uv (Hermes venv has no pip at build time)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Telethon from git source (Layer 232) + PySocks (provides 'import socks')
RUN uv pip install --no-cache --python /opt/hermes/.venv/bin/python \
    git+https://codeberg.org/Lonami/Telethon.git PySocks

# Add the Telethon platform plugin (already in HF space)
COPY plugins/platforms/telethon/ /opt/hermes/plugins/platforms/telethon/

# Fix: copy set_session_backend into delegation_backends.py
COPY agent_delegation_backends.py /opt/hermes/agent/delegation_backends.py

ENV HERMES_DASHBOARD=1 \
    HERMES_DASHBOARD_HOST=0.0.0.0 \
    HERMES_DASHBOARD_PORT=7860 \
    HERMES_HOME=/data \
    HERMES_WRITE_SAFE_ROOT=/data \
    PYTHONUNBUFFERED=1
EXPOSE 7860

CMD ["gateway", "run"]
