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

`scene-core/quality_report_v2.py` reopens the authority ledger instead of
trusting summary counts. It requires no unresolved non-level evidence, at least
one declared topology space, no current P0/P1 issue, current hash-bound measured
evidence files, a structured reviewer identity, and an exact three-digest
binding. Legacy V1 quality fields do not participate in this decision.
