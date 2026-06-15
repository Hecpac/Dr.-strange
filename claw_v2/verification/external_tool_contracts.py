"""F3b.0 — Tier-3 external tool contracts (offline mocks only).

Declarative SuccessCondition + PreflightSpec for tools that touch external
services. F3b.0 lands the contract+gate plumbing; the live external_check
runner (HTTP/CDP) is F3b.1. For now, tests exercise the gate against MOCKED
external_observation passed in directly — no real HeyGen / Telegram /
OpenAI / Anthropic / GitHub remote calls.

Coverage (priority order per Hector):
  1. HeyGenDeliver  — video poll + download + Telegram delivery
  2. HeyGenVideo    — video generation request
  3. GPTImage       — image generation
  4. SkillExecute   — execute a registered skill (Tier 3)
  5. A2ASend        — send a task to a peer agent
  6. WikiDelete     — cascade-delete a wiki entry (requires approval artifact)

NOT covered yet (deferred to F3b.1+):
  - X/LinkedIn publishing (Tier 3, browser/CDP — out of F3b.0 scope per restriction)
  - Live HTTP fetch runner for external_check

DEBT TRACKED (per Hector F3b.0 trailing notes):
  - allowed_path_roots=(workspace_root, /tmp) → replace with
    (workspace_root, artifact_root, session_tmp_root) when those are wired.
  - Bash command_kind=git_commit: add direct
    `forbidden_field_values={"remote_push_attempted": (True, "true", "yes")}`
    when that field starts appearing in the handler result.
"""

from __future__ import annotations


from claw_v2.verification.success_contract import (
    ExternalCheckSpec,
    FileIntegrityCheck,
    PreflightSpec,
    SuccessCondition,
)


# ---------------------------------------------------------------------------
# Tier-3 declarative contracts.
#
# Every contract MUST satisfy F3b.0 invariants:
#   - external_check declared OR an offline state_delta_check
#   - forbidden_reasons cover provider-specific failure modes
#   - sensitive args (prompts, image/video bytes, tokens) are NEVER persisted
#     in the artifact (the artifact builder redacts them — see
#     local_tool_contracts.build_local_tool_artifact).
# ---------------------------------------------------------------------------


EXTERNAL_TOOL_SUCCESS_CONDITIONS: dict[str, SuccessCondition] = {
    # 2. HeyGenVideo — submit a video generation job, return job id + status.
    "HeyGenVideo": SuccessCondition(
        must_contain_keys=("video_id", "status"),
        must_be_nonempty_str=("video_id",),
        must_match_regex={"video_id": r"^[A-Za-z0-9_\-]{16,64}$"},
        external_check=ExternalCheckSpec(
            kind="http_get_json",
            target="https://api.heygen.com/v1/video_status.get",
            json_path_equals={"status": "completed"},
        ),
        forbidden_reasons=(
            "api_error",
            "rate_limited",
            "invalid_api_key",
            "timeout",
            "content_policy_violation",
        ),
    ),
    # 1. HeyGenDeliver — poll job → download → ffmpeg compress → Telegram send.
    "HeyGenDeliver": SuccessCondition(
        must_contain_keys=("video_id", "output_path", "telegram_msg_id"),
        must_be_nonempty_str=("video_id", "output_path"),
        must_match_regex={
            "video_id": r"^[A-Za-z0-9_\-]{16,64}$",
            "telegram_msg_id": r"^\d+$",
        },
        must_be_existing_path=("output_path",),
        verify_file_integrity=(
            FileIntegrityCheck(
                path_field="output_path",
                hash_field="output_sha256",
                size_field="output_size_bytes",
            ),
        ),
        external_check=ExternalCheckSpec(
            kind="http_get_json",
            target="https://api.telegram.org/bot.../getMessage",
            json_path_equals={"ok": True},
        ),
        forbidden_reasons=(
            "video_not_ready",
            "download_failed",
            "telegram_send_failed",
            "telegram_size_exceeded",
            "compress_failed",
        ),
    ),
    # 3. GPTImage — generate an image from a prompt.
    "GPTImage": SuccessCondition(
        must_contain_keys=("output_path", "mime_type", "size_bytes"),
        must_be_nonempty_str=("output_path", "mime_type"),
        must_match_regex={"mime_type": r"^image/(png|jpe?g|webp|gif)$"},
        must_be_existing_path=("output_path",),
        verify_file_integrity=(
            FileIntegrityCheck(
                path_field="output_path",
                hash_field="output_sha256",
                size_field="size_bytes",
            ),
        ),
        forbidden_reasons=(
            "api_error",
            "content_policy_violation",
            "rate_limited",
            "invalid_api_key",
        ),
    ),
    # 4. SkillExecute — run a registered skill (Tier 3 because the skill may
    #    mutate external state). The contract only enforces the wrapper-level
    #    invariants; the skill itself is responsible for finer-grained checks.
    "SkillExecute": SuccessCondition(
        must_contain_keys=("skill_name", "execution_id"),
        must_be_nonempty_str=("skill_name", "execution_id"),
        forbidden_reasons=(
            "skill_not_found",
            "execution_error",
            "policy_violation",
            "outside_workspace",
        ),
    ),
    # 5. A2ASend — deliver a task to an A2A peer over HTTP.
    "A2ASend": SuccessCondition(
        must_contain_keys=("to_agent", "task_id", "delivered"),
        must_be_nonempty_str=("to_agent", "task_id"),
        must_equal={"delivered": True},
        external_check=ExternalCheckSpec(
            kind="http_get_json",
            target="https://peer.example.test/tasks/{task_id}",
            json_path_equals={"received": True},
        ),
        forbidden_reasons=(
            "peer_not_found",
            "delivery_failed",
            "auth_failed",
            "timeout",
        ),
    ),
    # 7. InstagramPublish — publish a local video as a Reel via CDP Chrome.
    #    Verified by Instagram's own share-confirmation modal (verified=True).
    #    No external_check: there is no clean HTTP endpoint; the in-flow modal
    #    is the authoritative signal and is reflected in the handler result.
    "InstagramPublish": SuccessCondition(
        must_contain_keys=("account", "shared", "verified"),
        must_be_nonempty_str=("account",),
        must_equal={"shared": True, "verified": True},
        forbidden_reasons=(
            "not_logged_in",
            "wrong_account",
            "video_not_found",
            "file_upload_failed",
            "caption_box_not_found",
            "share_button_not_found",
            "share_not_confirmed",
            "cdp_unavailable",
        ),
    ),
    # 6. WikiDelete — cascade-delete a wiki entry. Irreversible.
    #    The contract requires `approval_artifact` to be a non-empty string;
    #    the upstream approval gate is responsible for setting it.
    "WikiDelete": SuccessCondition(
        must_contain_keys=("slug", "deleted", "approval_artifact"),
        must_be_nonempty_str=("slug", "approval_artifact"),
        must_equal={"deleted": True},
        forbidden_reasons=(
            "approval_missing",
            "slug_not_found",
            "outside_workspace",
        ),
    ),
}


# ---------------------------------------------------------------------------
# Tier-3 preflight specs. Mandatory for irreversible / external-effect
# actions. The preflight runner is F3b.1; here we only declare the specs.
# ---------------------------------------------------------------------------


EXTERNAL_TOOL_PREFLIGHTS: dict[str, PreflightSpec] = {
    "HeyGenVideo": PreflightSpec(
        probe_kind="auth_check",
        target="HEYGEN_API_KEY",
        fail_message="HeyGen API key not present in Keychain.",
    ),
    "HeyGenDeliver": PreflightSpec(
        probe_kind="auth_check",
        target="HEYGEN_API_KEY+TELEGRAM_BOT_TOKEN",
        fail_message="Either HeyGen or Telegram credentials are missing.",
    ),
    "GPTImage": PreflightSpec(
        probe_kind="auth_check",
        target="OPENAI_API_KEY",
        fail_message="OpenAI API key not present.",
    ),
    "A2ASend": PreflightSpec(
        probe_kind="account_exists",
        target="peer_registry",
        fail_message="A2A peer not registered.",
    ),
    "SkillExecute": PreflightSpec(
        probe_kind="dry_run",
        target="skill_signature_check",
        fail_message="Skill argument signature mismatch.",
    ),
    "WikiDelete": PreflightSpec(
        probe_kind="dry_run",
        target="approval_artifact_present",
        must_match={"approval_artifact_present": True},
        fail_message="WikiDelete requires an explicit approval_artifact.",
    ),
    "InstagramPublish": PreflightSpec(
        probe_kind="account_exists",
        target="instagram_session",
        fail_message="Instagram session not logged in on the CDP Chrome profile.",
    ),
}


# ---------------------------------------------------------------------------
# Privacy: keys whose values are NEVER persisted into the artifact.
# Extends the local-tool redaction list. Applied by the artifact builder.
# ---------------------------------------------------------------------------


EXTERNAL_TOOL_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        # Local-tool keys (kept for completeness)
        "content",
        "old_text",
        "new_text",
        "image_bytes",
        "image_b64",
        "image_data",
        # Tier-3 additions
        "prompt",
        "prompt_text",
        "text",
        "image_url",
        "video_url",
        "audio_url",
        "video_bytes",
        "audio_bytes",
        "audio_b64",
        "api_key",
        "token",
        "secret",
        "password",
        "telegram_token",
        "openai_key",
        "anthropic_key",
    }
)


def get_external_success_condition(tool_name: str) -> SuccessCondition | None:
    return EXTERNAL_TOOL_SUCCESS_CONDITIONS.get(tool_name)


def get_external_preflight(tool_name: str) -> PreflightSpec | None:
    return EXTERNAL_TOOL_PREFLIGHTS.get(tool_name)
