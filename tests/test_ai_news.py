from __future__ import annotations

import unittest

from claw_v2.ai_news import (
    AiNewsBrief,
    AiNewsItem,
    ClaimEvidence,
    render_ai_news_brief,
    validate_ai_news_brief,
)
from claw_v2.verification_profiles import (
    PROFILES,
    verify_profile_evidence,
)


def _good_claim(**overrides) -> ClaimEvidence:
    base = ClaimEvidence(
        claim="Anthropic released X",
        source_title="Anthropic Blog",
        source_url="https://anthropic.com/news/x",
        source_kind="primary",
        published_at="2026-04-27",
        fetched_at="2026-04-27T08:00:00-05:00",
        verified_fields=["title", "url", "published_at"],
        confidence="high",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _good_item(**overrides) -> AiNewsItem:
    base = AiNewsItem(
        title="Anthropic ships Claude X",
        summary="Anthropic announced X today.",
        claims=[_good_claim()],
        confidence="high",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def _good_brief(**overrides) -> AiNewsBrief:
    base = AiNewsBrief(
        date="2026-04-27",
        timezone="America/Chicago",
        weekday="lunes",
        fetched_at="2026-04-27T08:00:00-05:00",
        items=[_good_item()],
        trend="modelos frontier siguen lanzando rápido",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class WeekdayValidationTests(unittest.TestCase):
    def test_2026_04_27_is_monday_not_sunday(self) -> None:
        brief = _good_brief(weekday="domingo")
        errors = validate_ai_news_brief(brief)
        self.assertIn("weekday_mismatch_for_date", errors)

    def test_correct_monday_label_passes(self) -> None:
        brief = _good_brief(weekday="lunes")
        errors = validate_ai_news_brief(brief)
        self.assertNotIn("weekday_mismatch_for_date", errors)

    def test_unknown_weekday_label_fails(self) -> None:
        brief = _good_brief(weekday="potato")
        errors = validate_ai_news_brief(brief)
        self.assertIn("unknown_weekday_label", errors)

    def test_invalid_date_returns_error(self) -> None:
        brief = _good_brief(date="not-a-date")
        errors = validate_ai_news_brief(brief)
        self.assertIn("invalid_date_or_timezone", errors)


class ClaimValidationTests(unittest.TestCase):
    def test_bad_weekday_is_validation_error(self) -> None:
        brief = _good_brief(weekday="sabado")
        errors = validate_ai_news_brief(brief)
        self.assertIn("weekday_mismatch_for_date", errors)

    def test_claim_without_source_is_validation_error(self) -> None:
        brief = _good_brief(items=[_good_item(claims=[_good_claim(source_url="")])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("claim_missing_source_url", errors)

    def test_claim_without_fetched_at_is_validation_error(self) -> None:
        brief = _good_brief(items=[_good_item(claims=[_good_claim(fetched_at="")])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("claim_missing_fetched_at", errors)

    def test_high_confidence_requires_verified_fields(self) -> None:
        bad_claim = _good_claim(verified_fields=[])
        brief = _good_brief(items=[_good_item(claims=[bad_claim])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("high_confidence_requires_verified_fields", errors)

    def test_high_confidence_requires_primary_or_wire_source_kind(self) -> None:
        bad_claim = _good_claim(source_kind="aggregator")
        brief = _good_brief(items=[_good_item(claims=[bad_claim])])
        errors = validate_ai_news_brief(brief)
        self.assertIn(
            "high_confidence_requires_primary_or_wire_source_kind",
            errors,
        )

    def test_aggregator_source_capped_at_medium_confidence(self) -> None:
        claim = _good_claim(source_kind="aggregator", confidence="high")
        brief = _good_brief(items=[_good_item(claims=[claim])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("aggregator_source_capped_at_medium", errors)

    def test_social_source_without_corroboration_capped_at_low(self) -> None:
        claim = _good_claim(source_kind="social", confidence="medium")
        brief = _good_brief(items=[_good_item(claims=[claim])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("social_source_without_corroboration_capped_at_low", errors)

    def test_unknown_source_kind_cannot_be_high(self) -> None:
        claim = _good_claim(source_kind="unknown", confidence="high")
        brief = _good_brief(items=[_good_item(claims=[claim])])
        errors = validate_ai_news_brief(brief)
        self.assertIn("unknown_source_kind_cannot_be_high", errors)

    def test_summary_says_today_without_evidence_fails(self) -> None:
        claim = _good_claim(published_at=None)
        item = _good_item(summary="Esto pasó de hoy.", claims=[claim])
        brief = _good_brief(items=[item])
        errors = validate_ai_news_brief(brief)
        # When published_at is None and no staleness label, summary saying
        # "de hoy" must be flagged.
        flagged = [e for e in errors if e.startswith("summary_says_today_without_evidence")]
        self.assertTrue(flagged, msg=f"errors: {errors}")

    def test_summary_says_today_with_staleness_label_passes(self) -> None:
        claim = _good_claim(published_at=None, unverified_fields=["not_today"])
        item = _good_item(summary="Hoy se confirmó.", claims=[claim])
        brief = _good_brief(items=[item])
        errors = validate_ai_news_brief(brief)
        flagged = [e for e in errors if e.startswith("summary_says_today_without_evidence")]
        self.assertFalse(flagged, msg=f"unexpected errors: {flagged}")


class RenderTests(unittest.TestCase):
    def test_render_marks_unverified_fields(self) -> None:
        item = _good_item(
            claims=[
                _good_claim(),
                ClaimEvidence(
                    claim="rumor",
                    source_title="Random Twitter",
                    source_url="https://x.com/rumor",
                    source_kind="social",
                    fetched_at="2026-04-27T08:00:00-05:00",
                    confidence="low",
                ),
            ]
        )
        brief = _good_brief(items=[item])
        rendered = render_ai_news_brief(brief)
        self.assertIn("AI Brief", rendered)
        self.assertIn("señal no verificada", rendered)


class ProfileTests(unittest.TestCase):
    def test_ai_news_profile_registered(self) -> None:
        self.assertIn("ai_news_brief", PROFILES)

    def test_ai_news_profile_requires_claim_map(self) -> None:
        decision = verify_profile_evidence(
            task_kind="ai_news_brief",
            evidence={"sources": [{"url": "x"}], "fetched_at": "2026-04-27T08:00"},
        )
        self.assertEqual(decision.status, "pending")
        self.assertIn("claim_map", decision.missing_evidence)

    def test_ai_news_profile_passes_with_full_evidence(self) -> None:
        decision = verify_profile_evidence(
            task_kind="ai_news_brief",
            evidence={
                "sources": [{"url": "x"}],
                "claim_map": {"a": "b"},
                "fetched_at": "2026-04-27T08:00",
            },
        )
        self.assertEqual(decision.status, "passed")


if __name__ == "__main__":
    unittest.main()
