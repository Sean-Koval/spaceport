FROM node:24.19.0-bookworm-slim@sha256:a9f5f7c91a432850b2a8a7797adf5eadb6c733ceed61167806cee7ea7fbc29df AS build
WORKDIR /app/publisher
COPY publisher/package.json publisher/package-lock.json ./
RUN test "$(node --version)" = v24.19.0 && npm install --global --ignore-scripts --no-audit --no-fund npm@11.9.0 && test "$(npm --version)" = 11.9.0 && npm ci --ignore-scripts --no-audit --no-fund
COPY publisher ./
COPY fixtures /app/fixtures
COPY docs/compatibility/openapi.json /app/docs/compatibility/openapi.json
RUN npm run build

FROM nginx:1.30.5-trixie@sha256:9bf97bd7714f5e24c1ccd545ecb9eb5435cb6d109c97cebb15e7e455e0239edb
COPY --from=build /app/publisher/dist /usr/share/nginx/html
COPY infra/edge.demo.conf /etc/nginx/nginx.conf
USER 101:101
ENTRYPOINT ["nginx","-g","daemon off;"]
