# Scene core instructions

- `scene_api.py` is the pure domain gate. CLI and MCP adapters must call it rather than duplicate geometry, evidence, identity, or acceptance rules.
- Every mutation validates the full Semantic Scene V2 schema and semantic invariants before atomic replacement.
- Stable errors use `CODE:message`; public MCP/CLI adapters add `retryable` and `nextActions` without exposing absolute paths.
- MCP transport, protocol negotiation, framing, capabilities, and structured-output validation belong to the official `mcp` SDK.
- Any change to Scene operations requires CLI/MCP parity tests, rejected-operation disk immutability tests, and a current reviewer-policy test.
