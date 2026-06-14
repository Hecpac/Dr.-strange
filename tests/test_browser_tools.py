from __future__ import annotations

import unittest

from claw_v2.browser_tools import (
    BrowserElementRef,
    BrowserToolResult,
    RawElement,
    RawPage,
)


class DataclassTests(unittest.TestCase):
    def test_result_defaults(self) -> None:
        r = BrowserToolResult(success=True, url="https://x.test", title="X")
        self.assertTrue(r.success)
        self.assertEqual(r.element_count, 0)
        self.assertEqual(r.backend, "chrome_cdp")
        self.assertEqual(r.metadata, {})

    def test_raw_page_carries_elements_and_login_flag(self) -> None:
        page = RawPage(
            url="https://x.test",
            title="X",
            text="hello",
            elements=[RawElement(selector="#post", role="button", label="Post",
                                 text="Post", href=None, input_type=None)],
            login_or_challenge=False,
        )
        self.assertEqual(len(page.elements), 1)
        self.assertEqual(page.elements[0].label, "Post")

    def test_element_ref_shape(self) -> None:
        ref = BrowserElementRef(ref="@e1", label="Post", role="button",
                                selector="#post", text="Post", href=None, input_type=None)
        self.assertEqual(ref.ref, "@e1")


if __name__ == "__main__":
    unittest.main()
