from __future__ import annotations

import unittest

from claw_v2.pricing import CostEstimate, estimate_cost_usd


class PricingTableTests(unittest.TestCase):
    def test_openai_known_model_reports_nonzero_cost(self) -> None:
        est = estimate_cost_usd("openai", "gpt-5.5", {"input_tokens": 100_000, "output_tokens": 100_000})
        self.assertIsInstance(est, CostEstimate)
        self.assertFalse(est.unknown)
        # 100k in @ $5/1M + 100k out @ $30/1M = 0.5 + 3.0
        self.assertAlmostEqual(est.amount_usd, 3.5)
        self.assertIsNotNone(est.price_source)
        self.assertEqual(est.price_as_of, "2026-06-01")

    def test_google_known_model_reports_nonzero_cost(self) -> None:
        est = estimate_cost_usd(
            "google", "gemini-2.5-pro", {"prompt_token_count": 100_000, "candidates_token_count": 100_000}
        )
        self.assertFalse(est.unknown)
        # under 200k: 100k in @ $1.25/1M + 100k out @ $10/1M = 0.125 + 1.0
        self.assertAlmostEqual(est.amount_usd, 1.125)

    def test_unknown_openai_model_sets_cost_unknown_true(self) -> None:
        est = estimate_cost_usd("openai", "gpt-9-ultra", {"input_tokens": 100, "output_tokens": 100})
        self.assertTrue(est.unknown)
        self.assertEqual(est.amount_usd, 0.0)
        self.assertIsNone(est.price_source)

    def test_unknown_google_model_sets_cost_unknown_true(self) -> None:
        est = estimate_cost_usd("google", "gemini-9-mega", {"prompt_token_count": 100, "candidates_token_count": 100})
        self.assertTrue(est.unknown)
        self.assertEqual(est.amount_usd, 0.0)

    def test_gemini_context_tier_above_threshold_uses_higher_rate(self) -> None:
        est = estimate_cost_usd(
            "google", "gemini-2.5-pro", {"prompt_token_count": 250_000, "candidates_token_count": 10_000}
        )
        self.assertFalse(est.unknown)
        # > 200k: 250k in @ $2.5/1M + 10k out @ $15/1M = 0.625 + 0.15
        self.assertAlmostEqual(est.amount_usd, 0.775)

    def test_usage_field_styles_openai_vs_google(self) -> None:
        # OpenAI Responses API uses input_tokens/output_tokens.
        openai_est = estimate_cost_usd("openai", "gpt-5.4-mini", {"input_tokens": 1_000, "output_tokens": 2_000})
        self.assertAlmostEqual(openai_est.amount_usd, (1_000 * 0.75 + 2_000 * 4.5) / 1_000_000)
        # Google usage_metadata uses prompt_token_count/candidates_token_count.
        google_est = estimate_cost_usd(
            "google", "gemini-2.5-pro", {"prompt_token_count": 1_000, "candidates_token_count": 2_000}
        )
        self.assertAlmostEqual(google_est.amount_usd, (1_000 * 1.25 + 2_000 * 10.0) / 1_000_000)

    def test_missing_usage_known_model_is_zero_but_not_unknown(self) -> None:
        # No tokens reported -> $0, but the model IS priced, so not cost_unknown.
        est = estimate_cost_usd("openai", "gpt-5.5", None)
        self.assertFalse(est.unknown)
        self.assertEqual(est.amount_usd, 0.0)


if __name__ == "__main__":
    unittest.main()
