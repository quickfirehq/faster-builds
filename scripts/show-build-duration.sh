#!/bin/zsh
# Shows how long each build took in the IDE's activity bar (step 1 of the guide).
# Usage: show-build-duration.sh [on|off]    (default: on)
# Restart the IDE afterwards.

set -euo pipefail

case "${1:-on}" in
  on)  value=YES ;;
  off) value=NO ;;
  *)   print -u2 "usage: ${0:t} [on|off]"; exit 2 ;;
esac

defaults write com.apple.dt.Xcode ShowBuildOperationDuration -bool "$value"
print "ShowBuildOperationDuration = $value. Restart the IDE to see the change."
