#!/usr/bin/env python3
"""Keep the production guide UI open against deterministic in-memory services."""

from __future__ import annotations

import sys

from gui_smoke import MainWindow, SmokeApplication


class AccessibilityFixtureApplication(SmokeApplication):
    def do_activate(self) -> None:
        # Start away from the alias-search result so the AT-SPI smoke proves
        # that filtering, rather than pre-existing selection, chose Nova Sport 6.
        self.state.values["guide_television_station_id"] = "tv.prima"
        self.main_window = MainWindow(self, self.state, self.services)
        self.main_window.present()
        self.main_window.select_page("guide")


if __name__ == "__main__":
    application = AccessibilityFixtureApplication()
    raise SystemExit(application.run([sys.argv[0]]))
