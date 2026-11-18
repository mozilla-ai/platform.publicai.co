# Otari with the Public AI overlay baked in, for Kubernetes (charts/platform/charts/otari
# in chat.publicai.co). Build from this directory: docker build -t <image> otari/
FROM mzdotai/otari:0.51.0

COPY --chown=otari:otari overlay /overlay

ENV PYTHONPATH=/overlay \
    OTARI_BOOTSTRAP=otari_publicai:register
