"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { assetDigest } = require("../../cube/bigquery/assets.cjs");
const root = path.resolve(__dirname, "../../cube/bigquery");
test("retained candidate pins controls/model/security/source separately from v1", () => {
  const bundle = JSON.parse(fs.readFileSync(path.join(root, "bundle.json")));
  const binding = JSON.parse(fs.readFileSync(path.join(root, "binding.json")));
  assert.equal(bundle.activation, "disabled");
  assert.equal(bundle.evidence, "offline-controls-only");
  assert.equal(bundle.asset_digest, assetDigest());
  assert.equal(binding.model_bundle_digest, bundle.asset_digest);
  assert.equal(binding.adapter_revision, "cube-rest-v2");
  assert.equal(binding.checkpoint_member, "WarehouseSales.source_loaded_at");
  assert.notEqual(
    binding.model_bundle_digest,
    JSON.parse(fs.readFileSync(path.join(root, "../bundles.json"))).consumer
      .bundle_digest,
  );
});
test("inline checkpoint is explicit UTC text and source join cannot fan out or hide missing rows", () => {
  const model = fs.readFileSync(path.join(root, "model/sales.yml"), "utf8");
  assert.match(model, /LEFT JOIN/);
  assert.match(model, /COUNT\(\*\) = 1/);
  assert.match(model, /ERROR\('Invalid source checkpoint'\)/);
  assert.match(model, /ERROR\('Missing source checkpoint'\)/);
  assert.match(
    model,
    /FORMAT_TIMESTAMP\('%Y-%m-%dT%H:%M:%E6SZ', w.loaded_at, 'UTC'\)/,
  );
  assert.match(
    model,
    /name: source_loaded_at\s+sql: source_loaded_at\s+type: string/,
  );
  assert.ok(!model.includes("pre_aggregations:"));
  assert.ok(!model.includes("CURRENT_TIMESTAMP"));
  assert.ok(!model.includes("refresh_key:"));
});
test("binding and independent semantic security agree on every member and scope role", () => {
  const binding = JSON.parse(fs.readFileSync(path.join(root, "binding.json")));
  const security = JSON.parse(
    fs.readFileSync(path.join(root, "security.json")),
  );
  const members = Object.fromEntries(
    Object.values(binding.fields).map((i) => [
      i.member,
      { kind: i.kind, type: i.cube_type },
    ]),
  );
  members[binding.scope.tenant_member] = { kind: "dimension", type: "string" };
  members[binding.scope.access_group_member] = {
    kind: "dimension",
    type: "string",
  };
  members[binding.checkpoint_member] = { kind: "dimension", type: "string" };
  assert.deepEqual(security.members, members);
  assert.equal(security.tenant_member, binding.scope.tenant_member);
  assert.equal(security.access_group_member, binding.scope.access_group_member);
  assert.equal(security.checkpoint_member, binding.checkpoint_member);
});
