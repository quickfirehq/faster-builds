# Find what makes your build slow

Scripts and a short guide for measuring a slow build in Xcode projects: see every build's time, time the same
build the same way, read the timing summary and the build timeline, and turn what you find into fixes.

"The build is slow" isn't something you can fix. "This script runs on every build" or "this one file takes twenty
seconds to type-check" is. The tools to tell the two apart are already in the IDE and in `xcodebuild`; these
scripts run them for you and print only the part you need.

The full article, with more on each step: [How to read your build timeline and find what's slow](https://quickfirebuild.com/blog/read-your-build-timeline/).

## The scripts

| Script | What it does | Guide step |
|---|---|---|
| [`show-build-duration.sh`](scripts/show-build-duration.sh) | Shows each build's duration in the IDE's activity bar | 1 |
| [`time-build.sh`](scripts/time-build.sh) | Times the same build several times and prints the median | 2 |
| [`timing-summary.sh`](scripts/timing-summary.sh) | Prints the build's time by kind of task, and the script phases that run on every build | 3, 5 |
| [`slow-type-checks.sh`](scripts/slow-type-checks.sh) | Lists the functions and expressions slowest to type-check | 5 |

They need macOS with Xcode installed (`xcode-select -p` shows which one runs) and nothing else: they're `zsh`
scripts, the shell every Mac has. None of them changes your project. Each takes your usual `xcodebuild`
arguments after `--`:

```sh
git clone https://github.com/quickfirehq/faster-builds.git
cd path/to/your/app
path/to/faster-builds/scripts/time-build.sh -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"
```

Add `-workspace MyApp.xcworkspace` or `-project MyApp.xcodeproj` when the folder has more than one. Run any
script with `-h` for its options. Tested with Xcode 27.1.

## Step 1: See every build's total time

```sh
scripts/show-build-duration.sh        # off again: scripts/show-build-duration.sh off
```

It runs `defaults write com.apple.dt.Xcode ShowBuildOperationDuration -bool YES`. Restart the IDE, and every build
shows how long it took in the activity bar. A week of glancing at that number tells you which kinds of change are
slow, and that's where to look first.

## Step 2: Measure the same build the same way

A single build time is noisy. Before changing anything, time a few standard builds at least three times each and
keep the middle value:

- **A build with no changes.** The floor: what the build system costs before any real work.
- **A one-line change inside a function body.** The most common edit, and usually the cheapest.
- **A change to a type that many files use.** A new property on a shared model, for example. This shows how far a
  change can spread.

`time-build.sh` runs one untimed build first, then the timed ones, and prints each time and the median:

```sh
scripts/time-build.sh -n 3 -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"
```

```
Warm-up build (not timed)...
Build 1 of 3: 1.03 s
Build 2 of 3: 1.04 s
Build 3 of 3: 1.13 s

Median: 1.04 s   (fastest 1.03 s, slowest 1.13 s, 3 builds)
```

(These times are from a tiny sample project; yours will be longer.)

For the two edit builds, `-c` runs a command before each timed build to make the change. One way: put a marker
line in the file you want to change, and let `sed` give it a new value each time.

```sh
# In a function body of Sources/Feature/ProfileView.swift, add:  let buildMarker = 0; _ = buildMarker
scripts/time-build.sh -c 'sed -i "" -E "s/buildMarker = [0-9]+/buildMarker = $RANDOM/" Sources/Feature/ProfileView.swift' \
  -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"

# In a type that many files use, add a property:  var buildMarker0: Int { 0 }
# Renaming it each time changes the type's interface, so the files that use the type rebuild too:
scripts/time-build.sh -c 'sed -i "" -E "s/var buildMarker[0-9]+:/var buildMarker$RANDOM:/" Sources/Model/User.swift' \
  -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"
```

Remove the marker when you're done (`git checkout -- <file>`). Use the same scheme, Simulator and configuration
each time, and don't measure while something else is compiling or indexing. Keep the numbers: every change you
make later is judged against them.

## Step 3: The timing summary

The timing summary shows how much time each kind of task took in total: compiling sources, linking, running
scripts, compiling asset catalogs, signing and so on. In the IDE it's Product › Perform Action › Build With
Timing Summary; from the command line, `xcodebuild -showBuildTimingSummary`. `timing-summary.sh` builds once with
it and prints only the summary:

```sh
scripts/timing-summary.sh -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"
```

```
Time by kind of task (summed across cores):
  SwiftCompile (1 task) | 0.228 seconds
  Ld (1 task) | 0.071 seconds
  SwiftDriver (1 task) | 0.046 seconds
  CodeSign (1 task) | 0.012 seconds
  PhaseScriptExecution (1 task) | 0.006 seconds
  ...

Script phases that run on every build (they declare no outputs):
  'Say hello', in target 'Demo' from project 'Demo'
```

It answers the first question: is the time going to the compiler, or to something around it? If script phases
or resource steps are a noticeable share of a one-line change, that's often the easiest win you'll find. For a
no-change build, run it twice and read the second summary: whatever still runs there runs on every build.

The totals add up work across all your Mac's cores, so they can be larger than the build's wall-clock time. Use
them to compare kinds of work, not as a stopwatch.

## Step 4: The build timeline

The timeline shows the same build as lanes of tasks over time. It's in the IDE, not the command line: open the
build in the report navigator, then show the assistant editor (Editor › Assistant) to see the timeline beside the
log. Each lane is a parallel slot on your Mac, and each block is a task: a compile, a link, a script, a copy, a
signature.

What to look for:

- **Long single blocks.** One compile task much longer than its neighbours usually means a file that is slow to
  type-check. Select it to see which file it is, or run `slow-type-checks.sh` (step 5).
- **Narrow stretches.** When most lanes are empty and one task is running, everything else is waiting for it. That
  task, or the chain of targets leading to it, sets the build's length. Splitting a large target, or removing a
  dependency that isn't needed, can let more work run side by side.
- **The same script every time.** A script block that shows up in a no-change build is running when it doesn't
  need to.
- **A long link at the end.** Linking can't start until everything it links is compiled, and it works on the whole
  app. If it's a large part of a one-line change, the app's size and the number of libraries it links are the
  levers.

## Step 5: Turn what you found into fixes

| What you see | What it usually means | What to try |
|---|---|---|
| A script runs in a no-change build, and the log warns that it "will be run during every build" (`timing-summary.sh` lists these) | The script phase declares no outputs | Add its input and output files, or untick "Based on dependency analysis" only for scripts that truly must always run |
| One file's compile is much longer than others | Slow type checking in that file | Run `slow-type-checks.sh` and fix what it lists, usually with an explicit type or a smaller expression |
| A small change to a shared type recompiles much of the app | Many files depend on that declaration | Move widely used types behind a small, stable interface, or into their own module |
| Most lanes idle while one target compiles | A dependency chain sets the pace | Check whether each target dependency is really needed; split very large targets |
| Asset catalogs or other resources rebuild every time | Something touches them on each build | Find the script or generated file that changes them |

`slow-type-checks.sh` builds with the compiler flags `-warn-long-function-bodies` and
`-warn-long-expression-type-checking` and lists every function and expression over the limit, slowest first:

```sh
scripts/slow-type-checks.sh -t 100 -- -scheme MyApp -destination "platform=iOS Simulator,name=iPhone 17"
```

```
Slower than 1 ms to type-check (slowest first):
      12 ms  Sources/Demo.swift:3  global function 'slow()'
       6 ms  Sources/Demo.swift:4  expression
```

(From the same sample project with `-t 1`; in a real app, start at 200 or 100.)

It builds into a temporary folder, so your normal build folder and settings stay as they are, but it's a full
build and takes as long as a clean one. To keep the warnings on in the IDE instead, add
`-Xfrontend -warn-long-function-bodies=200 -Xfrontend -warn-long-expression-type-checking=200` to Other Swift Flags
for the Debug configuration.

## What this won't show you

The timeline shows what ran, not whether it had to. A one-line change that recompiles one file, links the app,
signs it and installs it can still be a long wait on a large app, even when every task in the timeline is
"necessary". Measuring tells you how far fixes like these can take you, and where the floor is.

For the reasons a small change can mean a big build, read
[why a one-line change rebuilds so much of a big app](https://quickfirebuild.com/blog/why-small-changes-rebuild-so-much/).
More guides on builds and coding agents are on [the Quickfire blog](https://quickfirebuild.com/blog/).

## About

Written by the team behind [Quickfire](https://quickfirebuild.com/), which makes everyday builds of large apps
faster on the project you already have. Corrections and suggestions are welcome as issues or pull requests.

Scripts and text are under the [MIT license](LICENSE).

Apple, iOS, Xcode and Swift are trademarks of Apple Inc. This project is not affiliated with or endorsed by Apple.
