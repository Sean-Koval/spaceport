FROM cubejs/cube:v1.8.5@sha256:47c298a6cc725b4840d42587ee6164b45fa195c34ddc9f9da8c98b63847684d3
# Immutable upstream dependency tree; no build-time network or package changes.
ENV NODE_ENV=production CUBEJS_DEV_MODE=false PORT=4000 HOME=/tmp/spaceport-no-adc NODE_PATH=/cube/node_modules GATEWAY_BIGQUERY_RUNTIME_DIR=/cube/bigquery-runtime
COPY cube/bigquery-runtime/*.cjs /cube/bigquery-runtime/
COPY tests/runtime/bigquery-serving-guard.cjs tests/runtime/bigquery-serving-server.test.cjs tests/runtime/bigquery-serving-image-smoke.cjs /cube/probe/
COPY tests/runtime/bigquery-serving-bundle /cube/probe/bigquery-serving-bundle
WORKDIR /cube/conf
USER 65532:65532
# Strip only upstream inert image metadata. All auth/endpoint/runtime overrides
# remain present so the strict application guards reject them.
ENTRYPOINT ["/usr/bin/env","-u","CUBEJS_DOCKER_IMAGE_VERSION","-u","CUBEJS_DOCKER_IMAGE_TAG","node"]
CMD ["/cube/bigquery-runtime/server.cjs"]
