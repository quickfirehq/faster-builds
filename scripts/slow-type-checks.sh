#!/bin/zsh
# Lists the functions and expressions that take longest to type-check (step 5 of the guide).
#
# Usage: slow-type-checks.sh [-t MILLISECONDS] -- <xcodebuild arguments>
#
#   -t MILLISECONDS   report anything slower than this (default 200)
#
# It builds the whole project once with the compiler's -warn-long-function-bodies and
# -warn-long-expression-type-checking flags, into a separate, temporary build folder: your normal build
# folder and project settings are left alone, but this is a full build, so it takes as long as a clean build.
#
# Example:
#   slow-type-checks.sh -t 100 -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"

set -euo pipefail

limit=200
while getopts "t:h" opt; do
  case $opt in
    t) limit=$OPTARG ;;
    *) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
done
shift $((OPTIND - 1))
[[ ${1:-} == "--" ]] && shift
if (( $# == 0 )); then
  print -u2 "usage: ${0:t} [-t MILLISECONDS] -- <xcodebuild arguments>"
  exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/slow-type-checks.XXXXXX")
trap 'rm -rf "$work/DerivedData"' EXIT
log="$work/build.log"

flags="\$(inherited) -Xfrontend -warn-long-function-bodies=$limit -Xfrontend -warn-long-expression-type-checking=$limit"

print "Building everything into a temporary folder (this is a full build)..."
if ! xcodebuild build -derivedDataPath "$work/DerivedData" "$@" OTHER_SWIFT_FLAGS="$flags" > "$log" 2>&1; then
  print -u2 "The build failed. First errors (full log: $log):"
  { grep -m 10 -E '(^|: )(fatal )?error: ' "$log" || tail -n 20 "$log"; } >&2
  exit 1
fi

# Warnings look like: /path/File.swift:12:5: warning: instance method 'load()' took 250ms to type-check (limit: 200ms)
results=$(grep -E '^/.*: warning: .* took [0-9]+ms to type-check' "$log" | sort -u |
  sed -E 's/^(.*):([0-9]+):[0-9]+: warning: (.*) took ([0-9]+)ms to type-check.*/\4 \1:\2 \3/' |
  sort -rn -k1,1 || true)

if [[ -z $results ]]; then
  print "Nothing took longer than ${limit} ms to type-check."
else
  print
  print "Slower than ${limit} ms to type-check (slowest first):"
  print -l ${(f)results} | while read -r ms where what; do
    where=${where#$PWD/}; where=${where#${PWD#/private}/}   # paths relative to here when inside it
    printf "  %6s ms  %s  %s\n" "$ms" "$where" "$what"
  done
fi
print
print "Full log: $log"
