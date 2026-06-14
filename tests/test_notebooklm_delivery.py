import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from claw_v2 import notebooklm_delivery
from claw_v2.notebooklm import NotebookLMService
from claw_v2.notebooklm_delivery import FileDeliveryResult, NotebookLMDeliveryService
from claw_v2.jobs import JobService


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class NotebookLMDeliveryServiceTests(unittest.TestCase):
    def _write(self, tmp: str, name: str) -> Path:
        p = Path(tmp) / name
        p.write_bytes(b"binary-content")
        return p

    def test_audio_uses_send_audio(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = self._write(tmp, "overview.m4a")
            captured: dict = {}

            def fake_urlopen(req, timeout=0):
                captured["url"] = req.full_url
                return _fake_response({"ok": True, "result": {"message_id": 42}})

            with patch.object(notebooklm_delivery, "_load_env",
                              return_value={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "999"}), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = NotebookLMDeliveryService().send_to_telegram(audio, caption="hi")

            self.assertTrue(res.ok)
            self.assertEqual(res.method, "sendAudio")
            self.assertEqual(res.telegram_message_id, 42)
            self.assertTrue(captured["url"].endswith("/sendAudio"))

    def test_document_uses_send_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            doc = self._write(tmp, "report.pdf")
            captured: dict = {}

            def fake_urlopen(req, timeout=0):
                captured["url"] = req.full_url
                return _fake_response({"ok": True, "result": {"message_id": 7}})

            with patch.object(notebooklm_delivery, "_load_env",
                              return_value={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "999"}), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                res = NotebookLMDeliveryService().send_to_telegram(doc)

            self.assertTrue(res.ok)
            self.assertEqual(res.method, "sendDocument")
            self.assertTrue(captured["url"].endswith("/sendDocument"))

    def test_missing_token_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = self._write(tmp, "overview.m4a")
            with patch.object(notebooklm_delivery, "_load_env", return_value={}), \
                 patch.dict("os.environ", {}, clear=True):
                res = NotebookLMDeliveryService().send_to_telegram(audio)
            self.assertFalse(res.ok)
            self.assertEqual(res.error, "missing_token_or_chat_id")

    def test_missing_file_returns_error(self) -> None:
        res = NotebookLMDeliveryService().send_to_telegram(Path("/nope/x.m4a"))
        self.assertFalse(res.ok)
        self.assertEqual(res.error, "file_not_found")

    def test_explicit_chat_id_overrides_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audio = self._write(tmp, "overview.m4a")
            captured: dict = {}

            def fake_urlopen(req, timeout=0):
                captured["body"] = req.data
                return _fake_response({"ok": True, "result": {"message_id": 1}})

            with patch.object(notebooklm_delivery, "_load_env",
                              return_value={"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_ALLOWED_USER_ID": "999"}), \
                 patch("urllib.request.urlopen", side_effect=fake_urlopen):
                NotebookLMDeliveryService().send_to_telegram(audio, chat_id="12345")

            self.assertIn(b"12345", captured["body"])
            self.assertNotIn(b"\r\n\r\n999\r\n", captured["body"])


class RenderReportHtmlTests(unittest.TestCase):
    def test_first_text_is_title_headings_and_table(self) -> None:
        from claw_v2.notebooklm_delivery import render_report_html

        items = [
            {"kind": "text", "text": "El fin de una era"},
            {"kind": "text", "text": "Sección corta sin punto"},
            {"kind": "text", "text": "Un párrafo largo y normal que termina con un punto final."},
            {"kind": "table", "rows": [["A", "B"], ["1", "2"]]},
        ]
        title, doc = render_report_html(items, meta="meta line")
        self.assertEqual(title, "El fin de una era")
        self.assertIn("<h1>El fin de una era</h1>", doc)
        self.assertIn("<h2>Sección corta sin punto</h2>", doc)
        self.assertIn("<p>Un párrafo largo", doc)
        self.assertIn("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>", doc)
        self.assertIn('<meta charset="utf-8">', doc)

    def test_escapes_html_in_text(self) -> None:
        from claw_v2.notebooklm_delivery import render_report_html

        _t, doc = render_report_html([
            {"kind": "text", "text": "Title"},
            {"kind": "text", "text": "riesgo <script>alert(1)</script> de inyección en el cuerpo."},
        ])
        self.assertNotIn("<script>", doc)
        self.assertIn("&lt;script&gt;", doc)


class NotebookLMOrchestrationDeliveryTests(unittest.TestCase):
    def test_completion_delivers_ready_outputs_and_notifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notify = MagicMock()
            job_service = JobService(Path(tmp) / "claw.db")
            svc = NotebookLMService(notify=notify, job_service=job_service)

            svc._cdp_download_fn = lambda nb, kind: f"/tmp/{kind}_file"
            svc._cdp_report_blocks_fn = lambda nb: [
                {"kind": "text", "text": "Título"},
                {"kind": "text", "text": "Informe de prueba con suficiente cuerpo para entregar."},
            ]

            fake_delivery = MagicMock()
            sent: list[tuple] = []

            def fake_send(path, *, chat_id=None, caption=None, title=None):
                sent.append((str(path), chat_id, caption))
                return FileDeliveryResult(ok=True, file_path=str(path), method="sendAudio", telegram_message_id=55)

            fake_delivery.send_to_telegram.side_effect = fake_send
            svc._delivery = fake_delivery

            def fake_step(notebook_id, checkpoint, outputs):
                return {
                    "status": "completed",
                    "stage": "outputs_ready",
                    "evidence_uri": "artifacts/notebooklm/evidence.json",
                    "summary": {"audio_ready": True, "blog_ready": True},
                }

            svc._cdp_orchestrate_step_fn = fake_step
            svc.start_orchestration("nb-full-id", session_id="tg-574707975")

            processed = svc.poll_orchestrations(limit=1)

            self.assertEqual(processed, 1)
            job = job_service.list()[0]
            self.assertEqual(job.status, "completed")
            deliveries = job.result["deliveries"]
            self.assertEqual(len(deliveries), 2)
            self.assertTrue(all(d["ok"] for d in deliveries))
            self.assertEqual({d["kind"] for d in deliveries}, {"podcast", "blog"})
            # chat_id derived from session_id (tg- stripped)
            self.assertTrue(all(c == "574707975" for _, c, _ in sent))
            notify.assert_called_once()
            self.assertIn("Entregado al chat", notify.call_args.args[0])
            # the blog branch wrote a real .md; clean it up
            for path, _, _ in sent:
                p = Path(path)
                if p.name.startswith("informe_") and p.exists():
                    p.unlink()

    def test_completion_without_ready_outputs_skips_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notify = MagicMock()
            job_service = JobService(Path(tmp) / "claw.db")
            svc = NotebookLMService(notify=notify, job_service=job_service)
            svc._cdp_download_fn = lambda nb, kind: (_ for _ in ()).throw(AssertionError("should not download"))

            def fake_step(notebook_id, checkpoint, outputs):
                return {"status": "completed", "stage": "outputs_ready", "summary": {}}

            svc._cdp_orchestrate_step_fn = fake_step
            svc.start_orchestration("nb-full-id", session_id="tg-test")
            svc.poll_orchestrations(limit=1)

            job = job_service.list()[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.result["deliveries"], [])
            self.assertNotIn("Entregado al chat", notify.call_args.args[0])

    def test_obtain_report_path_writes_html(self) -> None:
        svc = NotebookLMService()
        svc._cdp_report_blocks_fn = lambda nb: [
            {"kind": "text", "text": "Título del informe"},
            {"kind": "text", "text": "Cuerpo del informe con acentos: Erdős y matemático."},
            {"kind": "table", "rows": [["Autor", "Valor"], ["OpenAI", "1+δ"]]},
        ]
        path = svc._obtain_report_path("7ab39ef0-xyz")
        try:
            self.assertIsNotNone(path)
            p = Path(path)
            self.assertTrue(p.exists())
            self.assertEqual(p.suffix, ".html")
            content = p.read_text(encoding="utf-8")
            self.assertIn('<meta charset="utf-8">', content)
            self.assertIn("<table>", content)
            self.assertIn("Cuerpo del informe", content)
            self.assertIn("<h1>Título del informe</h1>", content)
        finally:
            if path and Path(path).exists():
                Path(path).unlink()

    def test_obtain_report_path_none_when_empty(self) -> None:
        svc = NotebookLMService()
        svc._cdp_report_blocks_fn = lambda nb: None
        self.assertIsNone(svc._obtain_report_path("nb-id"))

    def test_delivery_failure_does_not_block_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notify = MagicMock()
            job_service = JobService(Path(tmp) / "claw.db")
            svc = NotebookLMService(notify=notify, job_service=job_service)
            svc._cdp_download_fn = lambda nb, kind: None  # no artifact path

            def fake_step(notebook_id, checkpoint, outputs):
                return {"status": "completed", "stage": "outputs_ready",
                        "summary": {"audio_ready": True}}

            svc._cdp_orchestrate_step_fn = fake_step
            svc.start_orchestration("nb-full-id", session_id="tg-test", outputs=("podcast",))
            svc.poll_orchestrations(limit=1)

            job = job_service.list()[0]
            self.assertEqual(job.status, "completed")
            self.assertEqual(job.result["deliveries"],
                             [{"kind": "podcast", "ok": False, "error": "no_artifact_path"}])
            notify.assert_called_once()


if __name__ == "__main__":
    unittest.main()
