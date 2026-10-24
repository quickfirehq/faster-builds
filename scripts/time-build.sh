#!/bin/zsh
# Times the same build several times and prints each time and the median (step 2 of the guide).
#
# Usage: time-build.sh [-n RUNS] [-c CHANGE_COMMAND] -- <xcodebuild arguments>
#
#   -n RUNS            timed builds to run (default 3)
#   -c CHANGE_COMMAND  shell command run before each timed build, to make the change you want to measure
#                      (without it, every timed build is a no-change build)
#
# One untimed build runs first, so the timed builds start from a finished build. Use the same scheme,
# destination and configuration every time, and don't measure while something else is building or indexing.
#
# Example, a no-change build:
#   time-build.sh -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"

set -euo pipefail
zmodload zsh/datetime

runs=3
change=""
while getopts "n:c:h" opt; do
  case $opt in
    n) runs=$OPTARG ;;
    c) change=$OPTARG ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[[ ${1:-} == "--" ]] && shift
if (( $# == 0 )); then
  print -u2 "usage: ${0:t} [-n RUNS] [-c CHANGE_COMMAND] -- <xcodebuild arguments>"
  exit 2
fi

logs=$(mktemp -d "${TMPDIR:-/tmp}/time-build.XXXXXX")

build() {
  local log=$1
  shift
  if ! xcodebuild build "$@" > "$log" 2>&1; then
    print -u2 "The build failed. First errors (full log: $log):"
    { grep -m 10 -E '(^|: )(fatal )?error: ' "$log" || tail -n 20 "$log"; } >&2
    exit 1
  fi
}

print "Warm-up build (not timed)..."
build "$logs/warmup.log" "$@"

durations=()
for i in {1..$runs}; do
  [[ -n $change ]] && eval "$change"
  start=$EPOCHREALTIME
  build "$logs/run-$i.log" "$@"
  elapsed=$(( EPOCHREALTIME - start ))
  durations+=$(printf "%.2f" $elapsed)
  print "Build $i of $runs: ${durations[-1]} s"
done

sorted=( $(print -l $durations | sort -n) )
count=${#sorted}
if (( count % 2 )); then
  median=${sorted[(count + 1) / 2]}
else
  median=$(printf "%.2f" $(( (sorted[count / 2] + sorted[count / 2 + 1]) / 2.0 )))
fi

print
print "Median: $median s   (fastest ${sorted[1]} s, slowest ${sorted[-1]} s, $count builds)"
print "Logs: $logs"
