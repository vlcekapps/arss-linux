#!/usr/bin/bash
set -euo pipefail

# Run the real GTK and AT-SPI smoke gates in a disposable X11 display.  The
# caller supplies a fresh D-Bus session with dbus-run-session so no desktop or
# accessibility state from the host is reused or changed.
display_number="${ARSS_XVFB_DISPLAY:-99}"
if [[ ! "${display_number}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ARSS_XVFB_DISPLAY must be a positive display number." >&2
    exit 2
fi

display=":${display_number}"
socket="/tmp/.X11-unix/X${display_number}"
if [[ -e "${socket}" ]]; then
    echo "The requested X display is already in use: ${display}" >&2
    exit 2
fi

Xvfb "${display}" -screen 0 1280x1024x24 -nolisten tcp -noreset &
xvfb_pid=$!

cleanup() {
    kill "${xvfb_pid}" 2>/dev/null || true
    wait "${xvfb_pid}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _attempt in {1..100}; do
    if [[ -S "${socket}" ]]; then
        break
    fi
    if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
        echo "Xvfb exited before the display became ready." >&2
        exit 1
    fi
    sleep 0.1
done
if [[ ! -S "${socket}" ]]; then
    echo "Xvfb did not become ready: ${display}" >&2
    exit 1
fi

export DISPLAY="${display}"
export GDK_BACKEND=x11
export NO_AT_BRIDGE=0

python3 -B tools/gui_smoke.py
python3 -B tools/large_text_smoke.py
python3 -B tools/accessibility_smoke.py
