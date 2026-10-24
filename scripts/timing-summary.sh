#!/bin/zsh
# Builds once with the timing summary and prints it, plus any script phase that runs on every build
# (steps 3 and 5 of the guide).
#
# Usage: timing-summary.sh -- <xcodebuild arguments>
#
# The summary adds up each kind of task across all your Mac's cores, so its totals can be larger than the
# build's wall-clock time: use it to compare kinds of work, not as a stopwatch.
# For a no-change build, run it twice and read the second summary.
#
# Example:
#   timing-summary.sh -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"

set -euo pipefail

[[ ${1:-} == "--" ]] && shift
if (( $# == 0 )) || [[ $1 == "-h" ]]; then
  print -u2 "usage: ${0:t} -- <xcodebuild arguments>"
  exit 2
fi

log=$(mktemp "${TMPDIR:-/tmp}/timing-summary.XXXXXX")
mv "$log" "$log.log" && log="$log.log"

print "Building..."
if ! xcodebuild build -showBuildTimingSummary "$@" > "$log" 2>&1; then
  print -u2 "The build failed. First errors (full log: $log):"
  { grep -m 10 -E '(^|: )(fatal )?error: ' "$log" || tail -n 20 "$log"; } >&2
  exit 1
fi

print
print "Time by kind of task (summed across cores):"
# The summary sits between "Build Timing Summary" and "** BUILD SUCCEEDED **", one "Kind (N tasks) | S seconds"
# line per kind of task.
sed -n '/^Build Timing Summary/,/^\*\* BUILD/p' "$log" | grep ' seconds$' | sed 's/^/  /'

always=$(grep -o "Run script build phase '[^']*' will be run during every build[^(]*([^)]*)" "$log" | sort -u || true)
if [[ -n $always ]]; then
  print
  print "Script phases that run on every build (they declare no outputs):"
  print -l ${(f)always} | sed -E "s/^Run script build phase ('[^']*').*\((.*)\)$/  \1, \2/"
fi

print
print "Full log: $log"
