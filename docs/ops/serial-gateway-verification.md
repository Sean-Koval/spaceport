# Bounded serial backend verification

The manual `Verify complete backend in bounded serial partitions` workflow is a
separate complete-backend acceptance path. The existing backend/demo workflow
and their default command behavior remain available. This workflow does not
establish the separate composed-demo acceptance result.

## Trust and preparation

Merge the independently reviewed CI-only bootstrap before using this workflow.
Dispatch the trusted workflow ref, with the exact reviewed candidate commit and
canonical manifest digest as its two inputs. A candidate's workflow files never
choose the running workflow, controller imports, cleanup scripts, phase commands,
or lane policy. The bootstrap is checked out at `github.workflow_sha`; candidate
code executes only as the ordinary reviewed workload under `contents: read`,
without persisted checkout credentials. This is import and orchestration
isolation, not an operating-system sandbox against intentionally malicious tests.

Regenerate a manifest from clean, separate bootstrap and candidate checkouts.
Install the candidate's locked Python dependencies for collection. Use a fresh
private directory outside both checkouts, with mode 0700. From any working
directory, invoke the absolute trusted driver with isolated, no-site Python:

```sh
REVIEWED_CONTROLLER_SHA=<exact-bootstrap-commit> \
REVIEWED_SOURCE_SHA=<exact-candidate-commit> \
  /absolute/trusted/.venv/bin/python -I -S \
  /absolute/trusted/scripts/ci_partition_driver.py manifest \
  --candidate /absolute/candidate --output /private/new-run/manifest.json
```

The command performs bounded real collection, verifies unchanged clean sources,
writes the canonical manifest privately, and prints its SHA256. Review the exact
manifest and candidate before dispatch. The manifest binds source commit/tree/
content digest, the complete trusted controller digest, and every selected node.
Any source, controller, marker, fixture, lane, or identity drift requires reviewed
regeneration. Unknown test files and missing critical runtime cases fail closed.
Do not hand-edit the manifest or reuse a digest from a different bootstrap.

## Acceptance and budgets

The explicit serial chain runs source, owners, execution, publishing,
registry/results, then the intact runtime/process/socket lane. Each backend job
has its own full pinned stack and dependency preparation. The source profile runs
once. A backend failure does not prevent later backend lanes from running after
a successful source job. A source failure prevents backend admission.

Caps remain source 180 seconds, each backend pytest 570 seconds, registry child 600,
backend stack at most 900 with at least 780 seconds required at admission, and each
test job 20 minutes. The controller reserves 120 seconds for exact registered cleanup
and 60 for finalization; insufficient remaining budget blocks new work. Aggregate
has 5 minutes. Maximum reservation is 125 runner-minutes. Recheck billing and current
lane measurements before dispatch; local SQL timings do not qualify hosted Cube,
process, socket or image behavior.

Only the always-run trusted aggregate can report full-backend PASS. It requires
six successful, same-repository/workflow/source/run/attempt receipts, the exact
disjoint full union, zero failed/skipped/error/duplicate/missing JUnit identities,
unchanged sources and tools, and verified cleanup. Partial reruns cannot pass;
rerun the complete workflow as a new attempt.

No artifacts, raw JUnit, child output, database descriptors or container
environments are uploaded. Fixed bounded output chunks carry closed metadata,
exact test identities and digests. Suppressed, missing, reordered, oversized or
truncated chunks block acceptance. Private diagnostics remain local to each
runner. Platform cancellation or runner loss can prevent cleanup/finalization;
such runs never qualify as successful acceptance.

### Failure-only diagnostics

The always-finalize step emits a bounded diagnostic JSON line when successful
acceptance evidence is unavailable. It exits nonzero and writes no acceptance
receipt or output chunks. The aggregate likewise reports a fixed failure when
manifest or receipt transport is missing or invalid. These reports cannot repair
missing proof or make a failed, cancelled, or skipped job pass.

Diagnostic attribution comes from the executing workflow context and clean
trusted checkout. Private context and cleanup records must match that exact job,
source, run, and attempt. Missing or invalid context exposes fixed status fields
only. Finalization uses the original first-step deadline; absent context or an
exhausted budget does not create another 60-second allowance.

Phase observations expose only closed supervision fields and bounded timing.
Test failures expose source-validated test identities without parameter payloads,
fixed stages and exception categories, and validated source coordinates. Source
tests require a completed, valid source collection; their manifest digest is the
dispatch pin because backend collection has not run yet. Backend test/progress
markers require the fully validated manifest and exact lane. Checkpoint and
nested backend-phase markers are allowed only for the backend stack. Client and
collection phases expose supervision metadata only. Raw logs, exception text,
result bodies, environment values and credentials stay private. Invalid details
are unavailable rather than copied into public output.
