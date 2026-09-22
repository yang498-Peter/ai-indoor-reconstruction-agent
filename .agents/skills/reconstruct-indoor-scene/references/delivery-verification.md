# Verify a static presentation release without overwriting other work

Use only when preparing or performing an authorized deployment, synchronizing downloads, or proving that other models remain unchanged. This is a presentation release check, not Semantic Scene V2 publication or an accuracy certificate. No server, credential, release path or capture data is a reusable default.

## Prepare one reviewable candidate

Keep the active demonstration usable while building into an isolated candidate. Inventory the live collection and current release first; capture the release identity and a complete relative-path-to-SHA256/size map. Preserve previous models and required fallback assets. Do not rebuild them merely because one flag changed.

Package from an explicit public asset allowlist, not a capture directory or repository dump. Internal observations, local paths and credentials stay outside. Export a separate customer-copy layer, but preserve all functional runtime fields: floor/drive surfaces, articulated pivots, colliders, reset poses, camera presets and localized object IDs used by this viewer. A sanitizer that retains only labels and bounds can produce a visually correct page whose driving floor is wrong.

Compare candidate runtime behavior with the local validated scene at the same sample positions and states. Check ground height at outdoor/indoor transitions, door state/collision, cabin camera and reset when those features exist. HTTP 200 and JSON parsing are insufficient. Validate assets and deep links under the real deployment prefix, including return links, textures, photos authorized for publication and downloads.

## Reusable inventory tool

The standard-library helper hashes a prepared static tree and compares exact file sets. It neither packages nor uploads files, reads no credentials, and grants no publication approval. Keep reports outside the tree being inventoried. It rejects link/junction entries, traversal names, case-colliding portable paths and malformed manifests.

```powershell
python .agents/skills/reconstruct-indoor-scene/scripts/delivery_inventory.py snapshot --root <baseline-static-root> --output <work>/baseline.json
python .agents/skills/reconstruct-indoor-scene/scripts/delivery_inventory.py snapshot --root <candidate-static-root> --output <work>/candidate.json
python .agents/skills/reconstruct-indoor-scene/scripts/delivery_inventory.py compare --before <work>/baseline.json --after <work>/candidate.json --allow ag/model.glb --allow ag/index.html --allow ag/preview.png
python .agents/skills/reconstruct-indoor-scene/scripts/delivery_inventory.py verify --root <candidate-static-root> --manifest <work>/candidate.json
```

Use exact paths from the intended change set; do not auto-approve every detected change. Added, changed and removed paths outside that set fail. Inventory comparison uses path-keyed dictionaries, not enumeration order; Windows and Linux sorting can differ. An allowed deletion still needs task authorization. The final verify detects missing, extra or changed files; a previous snapshot is stale after any mutation. Run verification again on the release tree immediately before activation. These checks assume a quiescent/frozen tree, not an atomic snapshot of concurrent writes.

## Activate within the user's authorized scope

1. Check the current destination release still matches the recorded baseline. Abort on concurrent change and reassess; never overwrite it blindly. Reuse explicit authorization for the same target/scope, without inventing a new approval round.
2. Prepare a new versioned release. Apply only the intended delta to a complete verified baseline, or upload a complete verified package. Reject absolute paths, traversal and symlink archive entries before extraction. Verify payload and final release hashes.
3. Use the platform's atomic release pointer switch where available. Several per-file replacements are not an atomic site update. Keep the previous release and a concrete rollback action. Do not change unrelated application configuration or loosen site-wide CSP to make one asset work.
4. Fetch actual-origin assets/downloads and compare with the release manifest; test gallery/deep links and the relevant real viewer interactions. An old cache can serve a plausible previous model. Verify existing application health/configuration remained unchanged. On failed postchecks, restore the prior release and report the failure rather than leaving a partly working candidate live.
5. Bind the final report to release identity, served hashes, actual URL, browser/viewport and checks performed. Keep local checks, public-origin checks, external BIM import and dimensional acceptance separate.

Authentication belongs in the existing credential mechanism or a transient hidden prompt, never command logs, source, screenshots, bundles or Git. Host identity verification remains enabled. A sandbox network denial is an environment restriction; inspect request failures before diagnosing a viewer or CSP defect. Stop after bounded failed attempts and state the exact remaining dependency.
