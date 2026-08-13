# Agent orchestration contract

Use this contract whenever geometry is edited or a scene is reviewed. The pipeline owns completion; an author cannot self-certify it.

## State model

The required public stages are:

1. `intake`
2. `evidence`
3. `seed`
4. `author`
5. `regional-review`
6. `global-review`
7. `publish`

`intake` is initialized from the fingerprint-bound job. Later stages may pass only after their prerequisites and required capabilities pass. Reopening or failing an earlier stage invalidates every later stage.

Each issue follows:

```text
OPEN -> PATCHED -> RESOLVED
  ^         |
  +-- FAIL--+
```

Every patch is bound to a valid semantic-scene JSON hash and copied to an immutable checkpoint. Every review is bound to the same hash plus hashed evidence files.

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

## Forced authoring rhythm

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
python scripts/reconstruction_loop.py capability --state <state> --actor root --name deterministic-render --status AVAILABLE --reason "local Three.js renderer" --evidence <viewer-or-render-script> --receipt <independent-probe.json>
python scripts/reconstruction_loop.py open-issue --state <state> --actor author-west --area west-wing --severity P1 --kind missing-wall --target Wall17 --summary "north return is absent" --evidence raw=generated/west-raw.png
python scripts/reconstruction_loop.py patch --state <state> --actor author-west --issue I0001 --scene <scene.json> --note "add measured return"
python scripts/reconstruction_loop.py review --state <state> --actor reviewer-east --issue I0001 --scene <scene.json> --verdict PASS --score 92 --evidence render=generated/west-model.png --evidence raw=generated/west-raw.png --note "return follows high returns"
```

Use `stage` to pass each stage with current artifacts. `seed`, `author`, `regional-review`, and `global-review` also require the current scene. Use `publish` only after `score_scene.py` passes.

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
