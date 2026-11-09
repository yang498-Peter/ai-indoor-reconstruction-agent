# Repository instructions

## Canonical contracts

- Semantic Scene V2 authority is defined by `scene-core/semantic-scene-v2.schema.json` and the semantic checks in `scene-core/scene_api.py`.
- The nine-stage workflow is defined by `schemas/pipeline-contract-v2.json`; generated pipeline state is not a second contract.
- Use `indoor-recon` as the public command surface. Legacy scripts are compatibility adapters during the package migration.
- Keep authority, hypothesis, and presentation artifacts separate. Hypothesis or presentation content cannot satisfy authority gates.

## Mutation and evidence rules

- Never hand-edit generated Scene, quality, review, benchmark, or publish artifacts. Change the producer and regenerate atomically.
- Never set a stage to `PASS` directly. Only its dedicated evaluator may do so.
- Do not weaken thresholds, skip checks, relabel `NOT_RUN`, or make checks non-blocking to obtain a green result.
- Authors may mutate authority. Reviewers are read-only and must have a distinct execution `runId` and checked-in tool policy.
- Raw/customer captures are read-only, stay outside Git, and must not be copied into fixtures or publish bundles.

## Editing and validation

- Read and write text as UTF-8. Prefer small `apply_patch` edits; do not reformat unrelated files.
- Inspect `git status --short` before edits and stage only files owned by the current work package.
- Run `python scripts/validate_portable_repo.py`, `python -m pytest -q`, and `node --test tests/scene-v2/*.test.mjs` after public contract changes.
- Run `python scripts/sync_skills.py --check`, package build, MCP smoke, synthetic E2E, and the quick benchmark when their surfaces change.
- Record implementation, files, validation, and remaining gates in the matching Chinese Markdown work record.

## Code review rules

- Flag any path that lets presentation or inferred data satisfy authority checks.
- Flag direct stage-PASS writes, author self-review, stale hash acceptance, non-atomic mutation, unstructured public errors, and legacy V1 auto-acceptance.
- Flag new thresholds without benchmark evidence and tests that pass by skipping or relaxing a blocking gate.
