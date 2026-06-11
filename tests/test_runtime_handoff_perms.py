from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import claw_v2.runtime_handoff as rh


class RuntimeHandoffSecretPermsTests(unittest.TestCase):
    def test_secret_created_atomically_not_via_write_text(self) -> None:
        # The signing secret must be created with 0o600 atomically (os.open),
        # NOT write_text-then-chmod, which leaves a window where the secret is
        # group/world readable under a permissive umask.
        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir)
            with patch.dict(os.environ, clear=False):
                os.environ.pop("RUNTIME_HANDOFF_SECRET", None)
                os.environ.pop("APPROVAL_SECRET", None)
                wrote_via_write_text = {"hit": False}
                orig_write_text = Path.write_text

                def spy(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                    if self.name == ".runtime_handoff_secret":
                        wrote_via_write_text["hit"] = True
                    return orig_write_text(self, *args, **kwargs)

                old_umask = os.umask(0o022)
                try:
                    with patch.object(Path, "write_text", spy):
                        secret = rh._queue_secret(queue_dir, None)
                finally:
                    os.umask(old_umask)

            self.assertTrue(secret)
            secret_file = queue_dir / ".runtime_handoff_secret"
            self.assertTrue(secret_file.exists())
            mode = secret_file.stat().st_mode & 0o777
            self.assertEqual(mode, 0o600, f"secret perms {oct(mode)} != 0o600")
            self.assertFalse(
                wrote_via_write_text["hit"],
                "secret must be created atomically via os.open, not write_text+chmod",
            )


if __name__ == "__main__":
    unittest.main()
