# Review receipt schema

Bind every Semantic Scene V2 review to separate geometry, evidence-set, and
artifact-byte digests. Do not use the legacy ambiguous `sceneSha256` field.

```json
{
  "schemaVersion": "2.0",
  "geometryDigest": "64 lowercase hex characters",
  "evidenceSetDigest": "64 lowercase hex characters",
  "artifactSha256": "64 lowercase hex characters",
  "reviewer": {
    "actorId": "regional-reviewer",
    "runId": "UUID or equivalent immutable execution id",
    "role": "reviewer",
    "provider": "codex|human|deterministic-checker"
  },
  "reviewedAt": "ISO-8601 timestamp",
  "p0": [],
  "p1": [],
  "p2": [],
  "requiredAreaIds": [
    "meeting-suite",
    "open-office-core",
    "west-wing",
    "east-wing",
    "south-service-band",
    "perimeter-envelope"
  ],
  "areas": [
    {
      "id": "meeting-suite",
      "score": 92,
      "evidence": [
        {"path": "generated/meeting-suite-high-structure-raw.png", "sha256": "64 lowercase hex characters"},
        {"path": "generated/meeting-suite-high-structure-grid.png", "sha256": "64 lowercase hex characters"}
      ],
      "notes": "Compared raw, overlay, elevation, Three.js and photos"
    }
  ]
}
```

List every required region exactly once. Evidence paths are resolved relative to
the receipt and their SHA-256 values are verified. `reviewer` is a structured
identity; `reviewedAt` must be a timezone-aware ISO-8601 timestamp that is not
in the future. Scores are integers from 0 to 100. A receipt with stale geometry,
evidence or artifact digests, P0/P1 findings, stale evidence, or a required area
below 85 cannot pass.

## Machine-enforced score gate

The receipt must carry `areas` (each entry `id` + integer `score`) and a
top-level integer `score` (the total). `scene-core/quality_report_v2.py`
enforces the SKILL promise directly: every area score must be at least 85 and
the total at least 90, reported in `checks.reviewScoreGate` /
`reviewMinAreaScore` / `reviewTotalScore` / `reviewAreaCount`. A receipt
without scores fails with `REVIEW_SCORE_MISSING`; a receipt below either
threshold fails with `REVIEW_SCORE_BELOW_GATE:<area=score,...>;total=<n>`. The
same 85/90 rule is recomputed as the `scoreGate` check of the
`global-review-receipt` stage artifact, whose payload therefore also carries
`areas` and `score`.

`scene-core/quality_report_v2.py` reopens the authority ledger instead of
trusting summary counts. It requires no unresolved non-level evidence, at least
one declared topology space, no current P0/P1 issue, current hash-bound measured
evidence files, a structured reviewer identity, and an exact three-digest
binding. Legacy V1 quality fields do not participate in this decision.

## Recomputed vs self-reported stage checks

`schemas/pipeline-contract-v2.json` classifies every `requiredArtifactChecks`
name in `checkProvenance`:

- **selfReported** (trusted human/author judgment): `viewerLoad`, `framing`,
  `joints`, `openings`, `requiredRegionsReviewed`, `currentRenderBound`, and
  the evidence/macro checks.
- **recomputed** (independently recomputed by `reconstruction_loop.py`
  evaluators; a self-reported `true` that recomputation contradicts fails the
  stage with `SELF_REPORTED_CHECK_MISMATCH`):
  - `authorityUnmodified` - the presentation receipt payload must record
    `sceneSha256`; it is compared to the pipeline `currentSceneSha256`.
  - `allEligibleProposalsDisposed` / `omission` - recomputed with
    `audit_structural_omissions.undisposed_eligible_candidates` against the
    current scene; the payload must provide `recompute.proposalsPath` and its
    disposition map (`dispositions` or `candidates[].disposition`); pending
    review dispositions do not count as disposals.
  - `topology` / `collisions` - the current scene is compiled through
    `scene-core/scene-core.js` in a node subprocess (120 s timeout); declared
    space boundaries must compile to accepted structures, and accepted solid
    walls must not cross interiors or overlap collinearly.
  - `support` - when accepted-measured walls exist, the payload must provide
    `recompute.indexPath` and `pointcloud_scene_metrics` recomputes the hard
    gate over the capture index.
  - `scoreGate` - the 85/90 rule over the receipt payload `areas`/`score`.

Missing recompute inputs fail closed with `CHECK_RECOMPUTE_INPUT_MISSING`; a
missing runtime (no node, missing libraries) fails closed with
`CHECK_RECOMPUTE_UNAVAILABLE`. There is no trust fallback.

## Evaluator versions and area-scoped re-review

Stage evaluations record `evaluatorVersion` from the per-evaluator
`EVALUATOR_VERSIONS` constants in `reconstruction_loop.py`; prerequisite
freshness compares these versions, so bumping one evaluator invalidates only
the stages that recorded it. `evaluatorCodeSha256` is still written, but as
provenance only.

`regional-review` keeps a per-area ledger (`areaReview` in the pipeline
state) fed by the receipt payload `areas`. `open-issue` scopes the regional
invalidation to its `--area` when that area is a recorded areaId (anything
else reopens every area), and `patch --affected-areas=a,b` reopens only the
listed areas - omitting the flag (or `all`) conservatively reopens all. The
stage itself still returns to PENDING, but the next regional review only has
to cover the areas that are not `PASS` in the ledger; untouched PASS records
survive. Presentation and global review remain whole-scene and are always
fully invalidated.
