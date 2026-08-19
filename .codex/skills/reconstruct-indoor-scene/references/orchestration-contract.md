# Agent orchestration contract

Use this contract whenever geometry is edited or a scene is reviewed. The pipeline owns completion; an author cannot self-certify it.

## State model

The required public stages are:

1. `intake`
2. `evidence`
3. `macro-hypothesis`
4. `seed`
5. `author`
6. `presentation-review`
7. `regional-review`
8. `global-review`
9. `publish`

`schemas/pipeline-contract-v2.json` is the single machine-readable source for stage order, dependencies, capabilities, typed artifacts, evaluators, and invalidation. `intake` is initialized from the fingerprint-bound job. Later stages may pass only after their dedicated evaluator verifies prerequisites, required capabilities, and current typed artifacts. The generic `stage` command cannot write `PASS`.

Pipeline state V2 binds the contract digest and uses compare-and-swap revision checks under an exclusive state lock. A V1 state fails closed with `PIPELINE_STATE_MIGRATION_REQUIRED`; run `migrate-state` explicitly. Migration preserves only the fingerprint-bound intake decision and resets downstream stages for typed reevaluation.

Each issue follows:

```text
OPEN -> PATCHED -> RESOLVED
  ^         |
  +-- FAIL--+
```

Every patch is bound to a valid semantic-scene JSON hash and copied to an immutable checkpoint. Every review is bound to the same hash plus hashed evidence files.

## Typed artifacts and invalidation

Every stage artifact is declared as `artifactType=path`. Except for the V2 scene authority, it uses the envelope in `schemas/pipeline-artifact-v1.schema.json`: job and capture identity, canonical payload digest, producer/version/git SHA, command, config and environment digests, seed, input hashes, and timestamp. Authority-bound review artifacts must include the current `scene-authority` artifact SHA in `inputs`.

Dependency invalidation follows the contract DAG, not list position. Authority changes return `author` to `REVIEW` and invalidate presentation, regional, global, and publish stages. Presentation-only or renderer-only changes invalidate presentation review and its downstream stages without invalidating evidence, macro hypothesis, seed, or author acceptance.

## Capability truth

Capabilities are `UNVERIFIED`, `AVAILABLE`, `DEGRADED`, or `BLOCKED`. `AVAILABLE` requires an existing evidence path such as the actual generator, viewer, measurement script, or review tool plus a separate probe receipt bound to its hash. The probe receipt records `capability`, `status=PASS`, `checkedBy`, timezone-aware `checkedAt`, a bounded `probeCommand` argv list, and `evidenceSha256s`; registration reruns the probe and requires the command to execute or syntax-check the registered tool. A missing capability must remain visible; never silently substitute a weaker method and call the stage normal.

Core capabilities:

- `point-cloud-sections`
- `semantic-scene-compiler`
- `semantic-edit`
- `deterministic-render`
- `visual-inspection`
- `topology-check`
- `overlap-check`
- `score-gate`

Photo association and material review are conditional. Geometry-only jobs may proceed, but whole-scene publication remains blocked until the job capabilities allow it.

## Macro pass before forced authoring

The first scene pass is not issue-sized. In one bounded macro pass, create global axes, a coherent shell or scan boundary, major spaces, circulation, continuous visual floor, primary wall families and furniture zones in `scene-hypothesis.json`. Render a first global top and oblique view. Strict evidence controls confidence and claims, but does not force the hypothesis or presentation to contain black holes.

Only after this pass does the issue-sized authority rhythm begin. `scene-authority.json` remains conservative; `scene-presentation.json` remains coherent and productized. Publication must identify which layer is being accepted.

## Forced authority rhythm

Work one issue at a time:

1. Inspect unannotated raw evidence.
2. Open one specific issue with target IDs and evidence.
3. Make the smallest justified semantic edit.
4. Checkpoint immediately while the scene still parses.
5. Regenerate deterministic raw, overlay, model, and local views.
6. Review with at least one render and one independent raw/overlay/elevation/photo artifact.
7. On failure, record the score and reopen the issue. After two non-improving attempts, change evidence, view, decomposition, or inference strategy before patching again.
8. Pass regional review before global review. A geometry change invalidates both.

Every actor uses a stable 3-64 character ASCII ID (`a-z`, digits, dot, underscore, hyphen). Every issue review must use a reviewer identity different from the patch author. P0/P1 additionally requires an independent regional or adversarial reviewer role. A reviewer inspects raw evidence before overlays and does not edit the authoritative scene during that review.

## Completion and recovery

- A compiling/parsing checkpoint is recoverable progress, not quality acceptance.
- Restore only an immutable checkpoint recorded in pipeline state.
- Do not publish with any unresolved issue, stale scene/review/score hash, blocked capability, incomplete stage, or geometry-only limitation.
- Publish copies the exact scene, receipt, and independently recomputed score into a new hash-addressed directory, refuses to overwrite it, and marks the files read-only. The manifest is tamper-evident, not a cryptographic access-control system; verify its hashes before delivery.

## Commands

Initialize through `init_reconstruction_job.py`, then use:

```powershell
python scripts/reconstruction_loop.py status --state <work>/pipeline-state.json
python scripts/reconstruction_loop.py migrate-state --state <work>/pipeline-state.json --actor migration-owner
python scripts/reconstruction_loop.py capability --state <state> --actor root --name deterministic-render --status AVAILABLE --reason "local Three.js renderer" --evidence <viewer-or-render-script> --receipt <independent-probe.json>
python scripts/reconstruction_loop.py evaluate-stage --state <state> --actor evidence-owner --name evidence --artifact evidence-bundle=<evidence-bundle.json> --note "indexed evidence complete"
python scripts/reconstruction_loop.py evaluate-stage --state <state> --actor macro-owner --name macro-hypothesis --artifact macro-hypothesis=<macro-hypothesis.json> --note "room-first topology proposed"
python scripts/reconstruction_loop.py evaluate-stage --state <state> --actor seed-owner --name seed --scene <scene-authority.json> --note "V2 authority seeded"
python scripts/reconstruction_loop.py open-issue --state <state> --actor author-west --area west-wing --severity P1 --kind missing-wall --target Wall17 --summary "north return is absent" --evidence raw=generated/west-raw.png
python scripts/reconstruction_loop.py patch --state <state> --actor author-west --issue I0001 --scene <scene.json> --note "add measured return"
python scripts/reconstruction_loop.py review --state <state> --actor reviewer-east --issue I0001 --scene <scene.json> --verdict PASS --score 92 --evidence render=generated/west-model.png --evidence raw=generated/west-raw.png --note "return follows high returns"
python scripts/reconstruction_loop.py invalidate --state <state> --actor presentation-owner --change presentation --reason "presentation artifact changed"
```

Use `stage` only for non-PASS operational states such as `IN_PROGRESS`, `REVIEW`, `BLOCKED`, or `FAILED`. Use `evaluate-stage` for `evidence` through `global-review`; the contract dispatches a dedicated evaluator and records its code hash and artifact-set digest. `seed` and all authority-bound review stages require the current V2 scene. Use the dedicated `publish --quality-report` command only after the V2 quality evaluator passes.

Example probe receipt:

```json
{
  "capability": "deterministic-render",
  "status": "PASS",
  "checkedBy": "independent-tool-reviewer",
  "checkedAt": "2026-08-12T01:00:00+00:00",
  "probeCommand": ["python", "tools/render_smoke.py", "--check"],
  "evidenceSha256s": ["sha256 of the registered renderer or probe artifact"]
}
```

Keep the command bounded, non-interactive, local, and safe to rerun from the work directory.
