# Adversarial review

Use independent reviewers across regions. A reviewer must inspect raw artifacts before overlays and run under `reviewer-readonly-v1`; it may submit verdicts but cannot edit authority, evidence or author checkpoints. Independence requires distinct actor and execution run IDs. P0/P1 requires a regional or adversarial reviewer class.

## Attack checklist

- candidate count was forced to zero by accepting weak geometry;
- one evidence file, duplicate bytes or same-root derived crops were counted as independent sources;
- an accepted claim survived a geometry, hosted-opening, topology or coordinate-frame change;
- one orchestration run renamed its actor and reviewed its own output;
- accepted line lies on furniture, blinds, a ceiling remnant, or a parallel interior face;
- exterior versus interior facade face was confused;
- partition endpoints close mathematically but miss visible wall returns;
- repeated dividers are skewed, unequal, or collide with furniture;
- wall crosses a door, passage, alcove, or glazing opening;
- glass extends floor-to-ceiling despite an opaque head or sill;
- fake floor/ceiling envelope obscures the point cloud;
- duplicate coplanar elements cause z-fighting or excessive opacity;
- rejected geometry still renders through another path;
- room closure ignores unpublished, missing, or overlapping boundaries;
- unscanned space was guessed without an inference label;
- screenshots or scores belong to an older scene hash;
- one region was never inspected at useful scale.

## Severity

- P0: wrong coordinate mapping, gross room topology, destructive source handling, or fabricated evidence.
- P1: visible wrong/missing wall, crossed opening, major facade offset, strong skew, duplicate geometry, or stale acceptance.
- P2: material, trim, minor height, color, or presentation defect that does not alter topology.

Fail for any P0/P1. Correct, regenerate, invalidate prior review, and repeat.

## Scoring rubric

- plan alignment: 25
- height and opening construction: 15
- topology and shared endpoints: 15
- evidence truthfulness: 15
- materials and color: 10
- collision and regularity: 10
- global omissions: 10

Score every region separately. Do not hide a weak region inside a high average.
