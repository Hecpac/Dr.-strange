"""Click the first (OFF) camera switch in System Settings = Google Chrome.

We identified via AX that the four switches in the Camera pane are at:
  (1411,143) value=0   <- Chrome (OFF)  - target
  (1411,186) value=1   <- node
  (1411,229) value=1   <- python3.14
  (1411,272) value=1   <- zoom.us
"""
from __future__ import annotations

import sys
import time

import pyautogui


def main() -> int:
    target_x, target_y = 1429, 151
    print(f"clicking_chrome_toggle at ({target_x},{target_y})")
    time.sleep(0.5)
    pyautogui.click(target_x, target_y)
    time.sleep(1.2)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
