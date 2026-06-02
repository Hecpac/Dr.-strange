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

    def test_openai_context_tier_above_272k_uses_higher_rate(self) -> None:
        # Official OpenAI pricing (verified 2026-06-01): gpt-5.5 and gpt-5.4
        # prompts with >272K input tokens bill at 2x input / 1.5x output. The
        # base-rate-only table previously UNDER-billed these, contradicting the
        # "never under-bills the cost gates" invariant.
        gpt55 = estimate_cost_usd("openai", "gpt-5.5", {"input_tokens": 300_000, "output_tokens": 10_000})
        # > 272k: 300k in @ $10/1M + 10k out @ $45/1M = 3.0 + 0.45
        self.assertAlmostEqual(gpt55.amount_usd, 3.45)
        gpt54 = estimate_cost_usd("openai", "gpt-5.4", {"input_tokens": 300_000, "output_tokens": 10_000})
        # > 272k: 300k in @ $5/1M + 10k out @ $22.5/1M = 1.5 + 0.225
        self.assertAlmostEqual(gpt54.amount_usd, 1.725)

    def test_openai_context_tier_at_threshold_uses_base_rate(self) -> None:
        # Boundary: exactly 272K input is still base rate (tier is strict >).
        est = estimate_cost_usd("openai", "gpt-5.5", {"input_tokens": 272_000, "output_tokens": 10_000})
        # 272k in @ $5/1M + 10k out @ $30/1M = 1.36 + 0.30
        self.assertAlmostEqual(est.amount_usd, 1.66)

    def test_gpt_5_4_mini_has_no_context_tier(self) -> None:
        # gpt-5.4-mini has no >272K tier in official pricing; stays at base rate.
        est = estimate_cost_usd("openai", "gpt-5.4-mini", {"input_tokens": 300_000, "output_tokens": 10_000})
        # 300k in @ $0.75/1M + 10k out @ $4.5/1M = 0.225 + 0.045
        self.assertAlmostEqual(est.amount_usd, 0.27)

    def test_google_thinking_tokens_billed_as_output(self) -> None:
        # Gemini bills thinking tokens at the output rate, but reports them in
        # thoughts_token_count, separate from candidates_token_count (the visible
        # output). Output cost MUST include thinking or every reasoning response
        # is under-billed and the cost gate goes blind to it.
        est = estimate_cost_usd(
            "google", "gemini-2.5-pro",
            {"prompt_token_count": 100_000, "candidates_token_count": 20_000,
             "thoughts_token_count": 50_000, "total_token_count": 170_000},
        )
        self.assertFalse(est.unknown)
        # input 100k @ $1.25/1M + output (20k visible + 50k thinking) @ $10/1M
        # = 0.125 + 0.700
        self.assertAlmostEqual(est.amount_usd, 0.825)

    def test_google_thinking_billed_at_long_context_output_rate(self) -> None:
        # >200K prompt: thinking tokens bill at the above-threshold output rate.
        est = estimate_cost_usd(
            "google", "gemini-2.5-pro",
            {"prompt_token_count": 250_000, "candidates_token_count": 10_000,
             "thoughts_token_count": 40_000, "total_token_count": 300_000},
        )
        # > 200k: 250k in @ $2.5/1M + (10k + 40k) out @ $15/1M = 0.625 + 0.75
        self.assertAlmostEqual(est.amount_usd, 1.375)

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
