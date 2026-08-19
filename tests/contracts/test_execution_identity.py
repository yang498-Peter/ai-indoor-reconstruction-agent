from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "scene-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
spec = importlib.util.spec_from_file_location("execution_identity_contract", CORE / "execution_identity.py")
assert spec and spec.loader
identity_api = importlib.util.module_from_spec(spec)
sys.modules["execution_identity_contract"] = identity_api
spec.loader.exec_module(identity_api)


def make_identity(actor: str, run_id: str, role: str, policy_id: str, reviewer_class=None):
    value = {
        "schemaVersion": "1.0", "actorId": actor, "runId": run_id, "role": role,
        "provider": "contract-test", "model": "fixture", "policyId": policy_id,
        "toolPolicyHash": identity_api.policy_digest(policy_id),
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "attestation": {"issuer": "contract-test", "enforcementMode": "application-enforced"},
    }
    if reviewer_class:
        value["reviewerClass"] = reviewer_class
    return value


class ExecutionIdentityContractTest(unittest.TestCase):
    def test_same_run_with_different_actor_cannot_review(self) -> None:
        run_id = "11111111-1111-4111-8111-111111111111"
        author = make_identity("author-west", run_id, "author", "author-v1")
        reviewer = make_identity("reviewer-east", run_id, "reviewer", "reviewer-readonly-v1", "regional")
        with self.assertRaisesRegex(identity_api.IdentityError, "REVIEWER_RUN_NOT_INDEPENDENT"):
            identity_api.require_independent_reviewer(author, reviewer, severity="P1")

    def test_mutation_policy_cannot_impersonate_reviewer(self) -> None:
        reviewer = make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "author-v1", "regional",
        )
        with self.assertRaisesRegex(identity_api.IdentityError, "REVIEWER_TOOL_POLICY_NOT_READ_ONLY"):
            identity_api.normalize_identity(reviewer)

    def test_read_only_reviewer_cannot_mutate_pipeline_stage(self) -> None:
        reviewer = make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "reviewer-readonly-v1", "regional",
        )
        with self.assertRaisesRegex(identity_api.IdentityError, "EXECUTION_OPERATION_FORBIDDEN"):
            identity_api.require_operation(reviewer, "pipeline:update-stage")

    def test_policy_hash_tampering_fails(self) -> None:
        reviewer = make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "reviewer-readonly-v1", "regional",
        )
        reviewer["toolPolicyHash"] = "0" * 64
        with self.assertRaisesRegex(identity_api.IdentityError, "TOOL_POLICY_HASH_MISMATCH"):
            identity_api.normalize_identity(reviewer)

    def test_p1_requires_regional_or_adversarial_reviewer(self) -> None:
        author = make_identity(
            "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
        )
        reviewer = make_identity(
            "reviewer-east", "22222222-2222-4222-8222-222222222222",
            "reviewer", "reviewer-readonly-v1", "standard",
        )
        with self.assertRaisesRegex(identity_api.IdentityError, "P0_P1_INDEPENDENT_REVIEW_REQUIRED"):
            identity_api.require_independent_reviewer(author, reviewer, severity="P1")

    def test_deterministic_checker_exception_requires_exact_provider_and_current_inputs(self) -> None:
        author = make_identity(
            "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
        )
        checker = make_identity(
            "author-west",
            "33333333-3333-4333-8333-333333333333",
            "deterministic-checker",
            "deterministic-checker-v1",
            "deterministic",
        )
        checker["provider"] = "deterministic-checker"
        checker["deterministicBinding"] = {
            "codeSha256": "a" * 64,
            "configDigest": "b" * 64,
            "inputDigests": ["c" * 64],
        }
        with self.assertRaisesRegex(identity_api.IdentityError, "DETERMINISTIC_CHECKER_BINDING_STALE"):
            identity_api.require_independent_reviewer(
                author, checker, severity="P1", required_input_digests={"d" * 64},
            )
        accepted = identity_api.require_independent_reviewer(
            author, checker, severity="P1", required_input_digests={"c" * 64},
        )
        self.assertEqual(accepted["role"], "deterministic-checker")

    def test_deterministic_checker_still_requires_a_distinct_run(self) -> None:
        author = make_identity(
            "author-west", "11111111-1111-4111-8111-111111111111", "author", "author-v1",
        )
        checker = make_identity(
            "author-west",
            "11111111-1111-4111-8111-111111111111",
            "deterministic-checker",
            "deterministic-checker-v1",
            "deterministic",
        )
        checker["provider"] = "deterministic-checker"
        checker["deterministicBinding"] = {
            "codeSha256": "a" * 64,
            "configDigest": "b" * 64,
            "inputDigests": ["c" * 64],
        }
        with self.assertRaisesRegex(identity_api.IdentityError, "REVIEWER_RUN_NOT_INDEPENDENT"):
            identity_api.require_independent_reviewer(
                author, checker, required_input_digests={"c" * 64},
            )

    def test_deterministic_checker_provider_spoof_is_rejected(self) -> None:
        checker = make_identity(
            "checker",
            "33333333-3333-4333-8333-333333333333",
            "deterministic-checker",
            "deterministic-checker-v1",
            "deterministic",
        )
        checker["deterministicBinding"] = {
            "codeSha256": "a" * 64,
            "configDigest": "b" * 64,
            "inputDigests": ["c" * 64],
        }
        with self.assertRaisesRegex(identity_api.IdentityError, "DETERMINISTIC_CHECKER_IDENTITY_INVALID"):
            identity_api.normalize_identity(checker)


if __name__ == "__main__":
    unittest.main()
