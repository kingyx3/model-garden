import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_garden_ddq", ROOT / "scripts" / "ddq.py")
DDQ = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(DDQ)


class DdqAssuranceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = DDQ.load_yaml(ROOT / "assurance" / "ddq-catalog.yaml")
        cls.evidence = DDQ.load_yaml(ROOT / "assurance" / "evidence.yaml")
        cls.singapore = DDQ.load_yaml(ROOT / "assurance" / "singapore.yaml")

    def answer(self, question):
        return DDQ.answer_question(question, self.catalog, self.evidence, self.singapore)

    def test_model_training_question_is_conservative(self):
        result = self.answer("Do you use our customer prompts or outputs to train AI models?")
        self.assertEqual(result["id"], "AI-05")
        self.assertTrue(result["review_required"])
        self.assertIn("does not claim", result["answer"].lower())

    def test_certification_question_never_invents_certificate(self):
        result = self.answer("Are you SOC 2 or ISO 27001 certified?")
        self.assertEqual(result["id"], "GOV-01")
        self.assertEqual(result["assurance_state"], "review_required")
        self.assertIn("No ISO 27001", result["answer"])

    def test_pdpa_question_includes_singapore_mapping(self):
        result = self.answer("Are you compliant with the Singapore PDPA and who is your DPO?")
        self.assertEqual(result["id"], "PRI-01")
        ids = {source["id"] for source in result["singapore_guidance"]}
        self.assertIn("pdpa", ids)
        self.assertTrue(result["review_required"])

    def test_agent_authority_question_maps_to_agentic_framework(self):
        result = self.answer("How do you stop an AI agent from taking actions outside its authority?")
        self.assertEqual(result["id"], "AI-01")
        ids = {source["id"] for source in result["singapore_guidance"]}
        self.assertIn("imda_agentic_ai_2026", ids)
        self.assertIn("allow", result["answer"].lower())

    def test_unknown_question_requires_manual_review(self):
        result = self.answer("What is your preferred office coffee roast?")
        self.assertFalse(result["matched"])
        self.assertEqual(result["assurance_state"], "manual_review")

    def test_readiness_surfaces_unverified_controls(self):
        result = DDQ.readiness(self.evidence, self.singapore)
        statuses = {row["control"]: row["status"] for row in result["controls"]}
        self.assertEqual(statuses["formal_certifications"], "not_verified")
        self.assertEqual(statuses["penetration_testing"], "not_verified")
        self.assertGreater(len(result["recommended_next_evidence"]), 3)


if __name__ == "__main__":
    unittest.main()
