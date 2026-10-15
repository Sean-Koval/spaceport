"use strict";
const test = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path");
const root = path.resolve(__dirname, "../..");
test("runtime image derives exact reviewed Cube without installation or mutable base overrides", () => {
  const dockerfile = fs.readFileSync(
    path.join(root, "cube/bigquery-runtime/Dockerfile"),
    "utf8",
  );
  const image = JSON.parse(
    fs.readFileSync(path.join(root, "infra/compose.reviewed.yaml")),
  ).services["cube-reviewed"].image;
  assert.equal(dockerfile.match(/^FROM (.+)$/m)[1], image);
  assert.equal(/\b(?:npm|yarn|pip|apt|curl|wget)\b/.test(dockerfile), false);
  assert.match(dockerfile, /^USER 65532:65532$/m);
  assert.match(dockerfile, /^WORKDIR \/cube\/conf$/m);
  const entry = JSON.parse(dockerfile.match(/^ENTRYPOINT (.+)$/m)[1]);
  assert.deepEqual(entry, [
    "/usr/bin/env",
    "-u",
    "CUBEJS_DOCKER_IMAGE_VERSION",
    "-u",
    "CUBEJS_DOCKER_IMAGE_TAG",
    "node",
  ]);
  assert.deepEqual(JSON.parse(dockerfile.match(/^CMD (.+)$/m)[1]), [
    "/cube/bigquery-runtime/server.cjs",
  ]);
  assert.match(
    dockerfile,
    /COPY cube\/bigquery-runtime\/\*\.cjs \/cube\/bigquery-runtime\//,
  );
});
