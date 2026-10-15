"use strict";
// Candidate identity only: not a deployed Cube bundle or runtime attestation.
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const FILES = Object.freeze([
  "admission-ledger.cjs",
  "assets.cjs",
  "bounded-driver.cjs",
  "model/sales.yml",
  "runtime-contract.cjs",
  "security.json",
  "source.json",
]);
function assetDigest(directory = __dirname) {
  const hash = crypto.createHash("sha256");
  for (const name of FILES) {
    const bytes = fs.readFileSync(path.join(directory, name));
    hash.update(name + "\0" + bytes.length + "\0");
    hash.update(bytes);
  }
  return hash.digest("hex");
}
if (require.main === module) process.stdout.write(assetDigest() + "\n");
module.exports = { assetDigest, FILES };
