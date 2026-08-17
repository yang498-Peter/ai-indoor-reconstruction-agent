# Review receipt schema

Bind every review to the exact `scene.json` SHA-256.

```json
{
  "sceneSha256": "64 lowercase hex characters",
  "reviewer": "agent or human identifier",
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

List every region declared by `scene.areaReview.regionIds`, each exactly once. The example scene has six regions; an unfamiliar scene may define a different non-zero set. Evidence paths are resolved relative to the receipt and their SHA-256 values are verified. `reviewer` must be a non-empty identifier and `reviewedAt` must be a timezone-aware ISO-8601 timestamp that is not in the future. Scores are integers from 0 to 100. A receipt with missing/extra/duplicate areas, stale evidence, stale scene hash, P0/P1 findings, or any area below 85 cannot pass.

The scorer also reopens the scene evidence rather than trusting summary counts: every `accepted-inferred` structure needs an explicit `inferenceReason` plus at least two distinct, existing files in `evidence.inferenceEvidencePaths`. The scene itself must publish `pipeline.structures=PASS`, `pipeline.author=PASS`, `areaReview=PASS`, `declaredTopologyReview=PASS`, `overlapReview=PASS`, and all blocking quality loops as `PASS`. Advisory loops may remain `REVIEW` only when they explicitly set `blocking: false`.
