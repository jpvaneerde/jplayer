#!/bin/sh
# Chromium full screen on the display, inside cage (see jplay-kiosk.service).
BROWSER=$(command -v chromium || command -v chromium-browser)
PROFILE="${HOME:-/var/lib/jplay}/kiosk-browser"
mkdir -p "$PROFILE"
# A crash or power cut leaves "Chromium didn't shut down correctly" behind: clear it.
sed -i 's/"exited_cleanly":false/"exited_cleanly":true/; s/"exit_type":"[^"]*"/"exit_type":"Normal"/' \
  "$PROFILE/Default/Preferences" 2>/dev/null
exec "$BROWSER" --kiosk --ozone-platform=wayland --user-data-dir="$PROFILE" \
  --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble --disable-features=Translate \
  --password-store=basic --check-for-update-interval=31536000 --overscroll-history-navigation=0 \
  --disable-pinch --autoplay-policy=no-user-gesture-required \
  http://127.0.0.1:5000/
