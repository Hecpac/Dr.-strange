from __future__ import annotations

from claw_v2.bot import (
    BotService,
    _brain_content_references_evidence,
    _user_authoritatively_marked_done,
)


class TestUserAuthoritativelyMarkedDone:
    def test_ok_final_marca_done(self) -> None:
        assert _user_authoritatively_marked_done(
            "OK final: marca F3b.1, F3b.1.5 y F3b.1.5.1 como done. Reiniciado y smoke local pasó."
        )

    def test_marca_x_como_succeeded(self) -> None:
        assert _user_authoritatively_marked_done("OK final: marca F3b.0 como SUCCEEDED.")

    def test_marca_como_listo(self) -> None:
        assert _user_authoritatively_marked_done("marca la pista como listo, cerramos.")

    def test_deja_x_como_done(self) -> None:
        assert _user_authoritatively_marked_done("Deja la fase F2 como done y abrimos la 3.")

    def test_ya_quedo_done(self) -> None:
        assert _user_authoritatively_marked_done("ya quedó listo, sigamos.")

    def test_mark_as_done_english(self) -> None:
        assert _user_authoritatively_marked_done("mark phase F4 as done")

    def test_regular_action_request_does_not_match(self) -> None:
        assert not _user_authoritatively_marked_done("hazlo")
        assert not _user_authoritatively_marked_done("publica el thread")
        assert not _user_authoritatively_marked_done("Dale credential check")

    def test_empty_input(self) -> None:
        assert not _user_authoritatively_marked_done("")
        assert not _user_authoritatively_marked_done(None)  # type: ignore[arg-type]


class TestBrainContentReferencesEvidence:
    def test_artifacts_verification_path(self) -> None:
        assert _brain_content_references_evidence(
            "evidence en artifacts/verification/f3b2/20260526T210323_correlation.json"
        )

    def test_artifacts_heygen_path(self) -> None:
        assert _brain_content_references_evidence("descargado a artifacts/heygen/video_1779.mp4")

    def test_artifacts_x_sweep_path(self) -> None:
        assert _brain_content_references_evidence(
            "barrido completo en artifacts/x_sweep/x_sweep_1779832313.json"
        )

    def test_evidence_uri_inline(self) -> None:
        assert _brain_content_references_evidence(
            "status: blocked\nevidence_uri: artifacts/verification/f3b2/x.json"
        )

    def test_f3b_receipt_filename(self) -> None:
        assert _brain_content_references_evidence(
            "el receipt f3b1_reconcile_1779817374.log confirma 161/161 passed"
        )

    def test_checkpoint_marker(self) -> None:
        assert _brain_content_references_evidence("listo.\n**Checkpoint:**\n- todo verde")

    def test_correlation_id(self) -> None:
        assert _brain_content_references_evidence(
            "correlation_id: 1b6484baa11c41d2b78b874df3514f6f registrado"
        )

    def test_msg_id_telegram(self) -> None:
        assert _brain_content_references_evidence("enviado a Telegram msg_id 10891")

    def test_db_reference(self) -> None:
        assert _brain_content_references_evidence(
            "grant persisted in data/claw.db tabla capability_grants"
        )

    def test_plain_completion_claim_does_not_match(self) -> None:
        assert not _brain_content_references_evidence("Listo, lo hice")
        assert not _brain_content_references_evidence("Voy a arrancar el thread")
        assert not _brain_content_references_evidence("Publicado.")

    def test_empty_input(self) -> None:
        assert not _brain_content_references_evidence("")
        assert not _brain_content_references_evidence(None)  # type: ignore[arg-type]


class TestEvidenceGateTemplatesAreShort:
    def test_pending_evidence_response_is_short_and_actionable(self) -> None:
        bot = BotService.__new__(BotService)
        msg = bot._pending_evidence_response(task_id=None)
        assert len(msg) < 100
        assert "No lo marco como hecho todavía" not in msg
        assert "No arranqué nada todavía" not in msg
        assert "ejecuto" in msg.lower() or "evidencia" in msg.lower()

    def test_unexecuted_start_response_is_short_and_actionable(self) -> None:
        bot = BotService.__new__(BotService)
        msg = bot._unexecuted_start_response(task_id=None)
        assert len(msg) < 100
        assert "No arranqué nada todavía" not in msg
        assert "ejecuto" in msg.lower() or "evidencia" in msg.lower()
