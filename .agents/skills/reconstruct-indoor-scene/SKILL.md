---
name: reconstruct-indoor-scene
description: Reconstruct indoor point clouds and photos into evidence-bounded geometry and a polished interactive 3D webpage. Use for full-room modeling, panorama-guided completion, furniture and opening correction, fast presentation iterations, reusable modeling workflows, or strict Semantic Scene V2 review and publication. Route outdoor or mixed captures to the site-scene workflow.
---

# Reconstruct Indoor Scene

For outdoor or mixed captures, use [the site-scene workflow](../../../.codex/skills/reconstruct-site-scene/SKILL.md), applying its presentation route when the user requests a demonstration.

Treat algorithms as measurement services and the Agent as the author. Preserve three distinct outputs: strict measured authority, explicit inferred hypothesis, and customer-facing presentation. Presentation quality never upgrades an evidence claim.

## Select the requested outcome

For a new dataset, recurring correction, or workflow/SOP update, read [references/modeling-decisions.md](references/modeling-decisions.md) for evidence conflicts, local measurements and bounded tool recovery. The executable Chinese runbook is [室内建模 SOP](../../../docs/室内建模准确性与快速交付SOP.zh-CN.md). Keep operational details in these references, not capture-specific rules in the entrypoint.

For a webpage demonstration, visual reconstruction, or explicit permission to infer missing areas, first read [references/presentation-fast-path.md](references/presentation-fast-path.md). Use its cached evidence tools and room-first correction loop. Deliver an explicitly non-authoritative presentation, useful camera presets, source observations and limitations. This route does not require pretending the authority pipeline passed, and cannot satisfy its gates. Never reuse another capture's coordinates, decisions or screenshots.

For exhibition counters, screen/door corrections, supplied branding, automatic tours, or joining an existing model collection, read [references/exhibition-and-collection.md](references/exhibition-and-collection.md). It adds regional checks and reusable asset inspection only when those details apply.

For detailed furniture/corridor refinement, bilingual customer demonstrations, or synchronized model/download delivery, read [references/refinement-and-delivery.md](references/refinement-and-delivery.md). Apply only the relevant checks; keep measured accuracy and presentation acceptance distinct.

For exterior corrections, driving/cockpit interaction or promotional lettering, read only the matching section of [references/interactive-showcase-polish.md](references/interactive-showcase-polish.md). For an authorized server update or checking that other models stayed unchanged, use [references/delivery-verification.md](references/delivery-verification.md).

For measured authority, independent acceptance, evaluation or immutable publication, use the strict workflow below. If both outcomes are requested, keep their artifacts and completion statements separate. User corrections to names and observed semantics are inputs, not measured dimensions.

For presentation IFC exports and overlay in a BIM/point-cloud viewer, read [the source-frame BIM workflow](references/bim-source-overlay.md). It covers importer material limits, semantic parts, coordinate round trips and actual import/reopen checks.

## Strict authority workflow

Start from the machine state, not from remembered steps. First inspect the installed command surface. The packaged commands below apply when `indoor-recon --help` succeeds in this checkout's environment. A portable checkout without that package must use the checked-in [legacy authority workflow](../../../.codex/skills/reconstruct-indoor-scene/SKILL.md) and [orchestration contract](../../../.codex/skills/reconstruct-indoor-scene/references/orchestration-contract.md), starting with `python .codex/skills/reconstruct-indoor-scene/scripts/reconstruction_loop.py status --state <work>/pipeline-state.json`. Do not install an unrelated namesake package or treat missing packaging as permission to bypass gates.

```powershell
indoor-recon status --work <work> --json
indoor-recon next --work <work> --json
```

Execute only the typed next action returned by the workflow. Do not hand-edit Scene JSON, set a stage to `PASS`, weaken thresholds, mark a blocking check non-blocking, or let an author self-review. Keep raw capture data read-only and outside Git.

For a new indoor capture, require one explicit point cloud, unit, Z-up declaration, and local-frame or CRS identity:

```powershell
indoor-recon init --capture <capture> --work <work> --profile default-office-v1 --length-unit metre --up-axis Z --coordinate-reference <frame>
```

`BLOCKED_*`, `NOT_RUN`, and `HUMAN_REQUIRED` are honest outcomes. Missing photos permits geometry-only work but blocks material and whole-scene acceptance. Outdoor, mixed, or unknown domains must not be forced through this workflow.

Use the official MCP server for typed Scene operations:

```powershell
indoor-recon mcp --scene <work>/scene-authority.json --identity <execution-identity.json>
```

Author identities may mutate; reviewer identities are read-only and may submit verdicts. Every acceptance must bind the current geometry claim, evidence lineage, and an independent reviewer execution. After any authority change, regenerate affected renders and reviews before evaluation.

Finish with deterministic evaluation and immutable publication:

```powershell
indoor-recon evaluate --work <work>
indoor-recon publish --work <work> --output <publish-root> --actor <publisher> --identity <publisher-identity.json>
```

Publish only when all blocking checks pass. Deliver the three scene layers, evidence and render bundle, quality report, reviewer receipts, provenance, viewer, and explicit remaining limitations.
