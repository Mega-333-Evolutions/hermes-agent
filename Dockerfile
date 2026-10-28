FROM nousresearch/hermes-agent:latest
ENV HERMES_DASHBOARD=1 \
HERMES_DASHBOARD_HOST=0.0.0.0 \
HERMES_DASHBOARD_PORT=7860 \
HERMES_HOME=/data \
HERMES_WRITE_SAFE_ROOT=/data \
PYTHONUNBUFFERED=1
EXPOSE 7860

# Add the Telethon platform plugin (already in HF space)
COPY plugins/platforms/telethon/ /opt/hermes/plugins/platforms/telethon/

CMD ["gateway", "run"]
