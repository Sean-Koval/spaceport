# Test-only composition: same immutable upstream packages as the source runtime.
# The caller uses --network none and the original locked Python/package trees
# already prepared for Probe.Dockerfile. No mutable local image is a FROM input.
FROM cubejs/cube:v1.8.5@sha256:47c298a6cc725b4840d42587ee6164b45fa195c34ddc9f9da8c98b63847684d3
ARG RUNTIME_IMAGE_ID
ARG PROBE_IMAGE_ID
# Related qualification references only; these are not base-image provenance.
LABEL gateway.source-journey.runtime-reference=${RUNTIME_IMAGE_ID} \
      gateway.source-journey.probe-reference=${PROBE_IMAGE_ID}
COPY python /opt/python
COPY packages /opt/packages
COPY bigquery-runtime/*.cjs /cube/bigquery-runtime/
# Keep nonsecret production defaults explicit rather than inheriting them from
# a mutable locally tagged source-runtime image.
ENV NODE_ENV=production CUBEJS_DEV_MODE=false PORT=4000 HOME=/tmp/spaceport-no-adc NODE_PATH=/cube/node_modules GATEWAY_BIGQUERY_RUNTIME_DIR=/cube/bigquery-runtime
ENV PYTHONPATH=/app:/opt/packages PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
USER 65532:65532
# Fixed Python owns the descriptor. Its Node grandchild gets only role-scoped
# source configuration, without host/admin credentials or the parent stdin.
ENTRYPOINT ["/opt/python/bin/python3.12", "-m", "tests.integration.source_pilot_bigquery_image"]
CMD []
