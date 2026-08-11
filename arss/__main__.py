from __future__ import annotations

from collections.abc import Sequence
import sys


def main(argv: Sequence[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if supplied and supplied[0] == "--background-check":
        if len(supplied) != 2:
            print("Usage: arss --background-check {rss|podcast}", file=sys.stderr)
            return 2
        # Keep GTK, libadwaita and GStreamer out of systemd one-shot jobs.
        from .background import background_main

        return background_main(supplied[1])

    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk, Gtk

    from .application import ArssApplication

    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        print("ARSS requires a graphical GTK session.", file=sys.stderr)
        return 1
    smoke_test = "--smoke-test" in supplied
    arguments = [sys.argv[0], *(argument for argument in supplied if argument != "--smoke-test")]
    application = ArssApplication(smoke_test=smoke_test)
    result = application.run(arguments)
    if smoke_test and not application.smoke_succeeded:
        return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
