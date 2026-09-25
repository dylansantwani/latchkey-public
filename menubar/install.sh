#!/bin/sh
# Build LatchkeyBar, put it in ~/Applications, and start it the way it will start itself
# at login. Safe to run again after any change to main.swift.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
app="$HOME/Applications/LatchkeyBar.app"

mkdir -p "$app/Contents/MacOS"
cp "$here/LatchkeyBar/Info.plist" "$app/Contents/Info.plist"

echo "building $app"
swiftc -O -o "$app/Contents/MacOS/LatchkeyBar" "$here/LatchkeyBar/main.swift"
codesign --force --sign - "$app" 2>/dev/null || true

# One icon only: stop what is running, then let the login agent own it from here on.
# A killed process counts as an unsuccessful exit, so KeepAlive brings the new build
# straight back; kickstart is the belt for the case where it does not.
pkill -x LatchkeyBar 2>/dev/null || true
"$app/Contents/MacOS/LatchkeyBar" --login on
sleep 2
if [ -f "$HOME/Library/LaunchAgents/com.dylan.latchkeybar.plist" ]; then
    pgrep -x LatchkeyBar >/dev/null \
        || launchctl kickstart -k "gui/$(id -u)/com.dylan.latchkeybar"
else
    open -a "$app"
fi

sleep 3
echo "menu bar status: $(cat /tmp/latchkeybar.status 2>/dev/null || echo 'no status file yet')"
