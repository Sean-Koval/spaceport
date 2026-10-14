#!/usr/bin/env python3
"""Hash exact reviewed Cube config/security/model bytes, with framed filenames.

The endpoint mounts its selected models at model/, so both source directories
use model/ as the canonical path. No credentials or operator URLs are hashed.
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def bundle_digest(model_directory: Path) -> str:
    files = [(name, ROOT / "cube" / name) for name in ("cube.js", "security.js")]
    names = sorted(path.name for path in model_directory.iterdir())
    if names != ["orders.yml", "watermark.yml"]:
        raise ValueError("Only the two reviewed model files belong to a bundle")
    files.extend(("model/" + name, model_directory / name) for name in names)
    digest = hashlib.sha256()
    for name, path in files:
        contents = path.read_bytes()
        digest.update((name + "\0" + str(len(contents)) + "\0").encode())
        digest.update(contents)
    return digest.hexdigest()


if __name__ == "__main__":
    print(json.dumps({name: bundle_digest(ROOT / "cube" / model) for name, model in
        [("consumer", "model"), ("fixture_test", "test-model")]}, sort_keys=True))
