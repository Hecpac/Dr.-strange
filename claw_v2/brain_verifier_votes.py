from __future__ import annotations

from html import escape

from claw_v2.brain_json import (
    _as_string_list,
    _clamp_confidence,
    _normalize_recommendation,
    _normalize_risk_level,
    _try_parse_json_object,
)


def _format_verifier_evidence(*, plan: str, diff: str, test_output: str) -> dict[str, str]:
    return {
        "evidence": "\n".join(
            [
                "<evidence>",
                f"<plan>{escape(plan, quote=False)}</plan>",
                f"<diff>{escape(diff, quote=False)}</diff>",
                f"<test_output>{escape(test_output, quote=False)}</test_output>",
                "</evidence>",
            ]
        )
    }


def _parse_verifier_payload(content: str) -> dict:
    parsed = _try_parse_json_object(content)
    if parsed is None:
        lowered = content.lower()
        recommendation = "approve"
        if "deny" in lowered or "do not proceed" in lowered or "should not proceed" in lowered:
            recommendation = "deny"
        elif "approval" in lowered or "review" in lowered or "human" in lowered:
            recommendation = "needs_approval"
        risk_level = "medium"
        for candidate in ("critical", "high", "medium", "low"):
            if candidate in lowered:
                risk_level = candidate
                break
        summary = content.strip().splitlines()[0] if content.strip() else "Verifier returned no content."
        return {
            "recommendation": recommendation,
            "risk_level": risk_level,
            "summary": summary,
            "reasons": [summary],
            "blockers": [],
            "missing_checks": [],
            "confidence": 0.3,
        }

    recommendation = _normalize_recommendation(parsed.get("recommendation"))
    risk_level = _normalize_risk_level(parsed.get("risk_level"))
    reasons = _as_string_list(parsed.get("reasons"))
    blockers = _as_string_list(parsed.get("blockers"))
    missing_checks = _as_string_list(parsed.get("missing_checks"))
    summary = str(parsed.get("summary") or "").strip() or "Verifier returned no summary."
    confidence = _clamp_confidence(parsed.get("confidence"))

    if recommendation == "approve" and (blockers or missing_checks or risk_level in {"high", "critical"}):
        recommendation = "needs_approval"

    return {
        "recommendation": recommendation,
        "risk_level": risk_level,
        "summary": summary,
        "reasons": reasons,
        "blockers": blockers,
        "missing_checks": missing_checks,
        "confidence": confidence,
    }


def _aggregate_verifier_votes(votes: list[dict]) -> dict:
    clean_votes = [vote for vote in votes if not vote.get("error")]
    all_votes = votes or []
    blockers = _merge_vote_lists(all_votes, "blockers")
    missing_checks = _merge_vote_lists(all_votes, "missing_checks")
    reasons = _merge_vote_lists(all_votes, "reasons")
    if not reasons:
        reasons = [str(vote.get("summary", "")).strip() for vote in all_votes if str(vote.get("summary", "")).strip()]
    has_error = any(bool(vote.get("error")) for vote in all_votes)
    recommendations = {vote.get("recommendation") for vote in clean_votes}
    risk_levels = [str(vote.get("risk_level", "medium")) for vote in all_votes]
    highest_risk = max(risk_levels or ["medium"], key=_risk_rank)
    unanimous_approve = (
        len(clean_votes) >= 2
        and len(clean_votes) == len(all_votes)
        and recommendations == {"approve"}
        and highest_risk in {"low", "medium"}
        and not blockers
        and not missing_checks
    )
    if unanimous_approve:
        return {
            "recommendation": "approve",
            "risk_level": highest_risk,
            "summary": "Verifier consensus approved the action.",
            "reasons": reasons,
            "blockers": [],
            "missing_checks": [],
            "confidence": _average_confidence(clean_votes),
            "consensus_status": "unanimous_approve",
        }
    consensus_status = "verifier_error" if has_error else "disagreement"
    summary_parts = [str(vote.get("summary", "")).strip() for vote in all_votes if str(vote.get("summary", "")).strip()]
    summary = "Verifier consensus requires human review."
    if summary_parts:
        summary = f"{summary} " + " | ".join(summary_parts[:2])
    return {
        "recommendation": "needs_approval",
        "risk_level": highest_risk if highest_risk in {"high", "critical"} else "high",
        "summary": summary,
        "reasons": reasons,
        "blockers": blockers,
        "missing_checks": missing_checks,
        "confidence": _average_confidence(clean_votes),
        "consensus_status": consensus_status,
    }


def _verifier_error_vote(*, role: str, provider: str, model: str, error: str) -> dict:
    return {
        "role": role,
        "provider": provider,
        "model": model,
        "requested_provider": provider,
        "requested_model": model,
        "recommendation": "needs_approval",
        "risk_level": "high",
        "summary": f"{role} verifier unavailable: {error}",
        "reasons": [f"{role} verifier unavailable"],
        "blockers": ["Verifier consensus incomplete"],
        "missing_checks": [],
        "confidence": 0.0,
        "response": None,
        "error": error,
    }


def _serializable_verifier_votes(votes: list[dict]) -> list[dict]:
    payload: list[dict] = []
    for vote in votes:
        payload.append(
            {
                "role": vote.get("role"),
                "provider": vote.get("provider"),
                "model": vote.get("model"),
                "requested_provider": vote.get("requested_provider"),
                "requested_model": vote.get("requested_model"),
                "recommendation": vote.get("recommendation"),
                "risk_level": vote.get("risk_level"),
                "summary": vote.get("summary"),
                "reasons": vote.get("reasons") or [],
                "blockers": vote.get("blockers") or [],
                "missing_checks": vote.get("missing_checks") or [],
                "confidence": vote.get("confidence", 0.0),
                "degraded_mode": bool(vote.get("degraded_mode", False)),
                "error": vote.get("error") or "",
            }
        )
    return payload


def _merge_vote_lists(votes: list[dict], key: str) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for vote in votes:
        for item in _as_string_list(vote.get(key)):
            if item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


def _average_confidence(votes: list[dict]) -> float:
    if not votes:
        return 0.0
    return round(sum(float(vote.get("confidence") or 0.0) for vote in votes) / len(votes), 3)


def _risk_rank(value: str) -> int:
    return {"low": 0, "medium": 1, "high": 2, "critical": 3}.get(value, 1)
