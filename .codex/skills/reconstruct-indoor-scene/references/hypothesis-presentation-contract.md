# Hypothesis and presentation contract

Use this contract to prevent a strict evidence graph from becoming an unreadable delivery model.

## Layer contract

### Authority

- Stores source-metre measurements, issues, evidence hashes and decisions.
- Fails closed for claims and publication.
- Never invents a wall, room, opening or furniture coordinate.
- May remain incomplete without forcing the visible scene to remain incomplete.

### Reconstruction Hypothesis

- Starts with global axes, shell, major spaces, passages, furniture zones and scan boundaries.
- Allows reasonable completion from repeated modules, architecture, nearby returns and posed-photo semantics.
- Every inferred object records `confidence`, `confidenceInterval`, `inferenceReason` and `authorityRefs`.
- Must remain spatially coherent even when evidence is sparse.
- Contradictory evidence changes or removes the hypothesis; missing evidence only lowers confidence.

### Presentation

- References hypothesis logical IDs rather than raw authority parts.
- Groups furniture into logical families with collapsed children.
- Uses continuous visual floors, readable cutaways, coherent materials, lighting, contact shadows and professional camera framing.
- May simplify hidden construction but cannot change the visible type, count, approximate location or orientation without recording the deviation.
- Never earns authority, topology or measurement credit.

## Required macro pass

The initial bounded pass produces all of the following before local issue iteration:

1. dominant axis families;
2. coherent outer or scan-bounded envelope;
3. major space polygons and connectivity;
4. continuous visual floor polygons;
5. primary wall and facade bands;
6. furniture and circulation zones;
7. repeated layout modules;
8. inferred scan continuations.

Repeated furniture starts on the closest global axis or its perpendicular. Store the observed yaw separately, then refine it after the family reads as one layout. A noisy per-object PCA is never allowed to make a repeated desk family visibly crooked.

The result may be approximate. It may not be a collection of isolated accepted fragments with zero readable spaces.

## Confidence vocabulary

- `measured`: direct point-cloud geometry with a bounded residual.
- `supported-inferred`: coherent completion supported by at least one geometric family plus photo, topology or repetition.
- `visual-inferred`: presentation completion needed for readability but not eligible for structural claims.
- `withheld`: contradictory or too ambiguous even for a coherent hypothesis.

Evidence mode distinguishes the last three with styling. Presentation mode may render measured and inferred geometry together, while the inspector preserves their confidence.

## Visual rejection gate

Reject the Presentation artifact if any condition is true:

- `spaceCount == 0`;
- unexplained disconnected floor components;
- floating primary walls or accidental endpoint gaps;
- isolated primary-wall ratio above 20 percent;
- occupied black-void ratio above 12 percent in the fitted plan frame;
- projected model fill below 65 percent;
- exposed internal component IDs;
- ungrouped componentized furniture;
- `EVIDENCE_ONLY` delivery label;
- a blind reviewer cannot describe the principal spaces and circulation in one sentence.

The gate is visual and semantic. Passing it does not imply centimetre accuracy.

## Efficient correction rhythm

1. Render global top and oblique from the hypothesis.
2. Fix the largest visual or spatial contradiction first.
3. Re-render once per coherent batch, not once per endpoint.
4. Use point-cloud slices to refine the selected region.
5. Use photos to refine type, material and missing visible components.
6. Stop after three regional cycles and reassess the macro hypothesis.
7. Run a real browser only after browser-free top and oblique pass.
