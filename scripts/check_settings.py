#!/usr/bin/env python3
"""Checks the build settings that decide how long a debug build takes, for every target in your project.

It reads the settings of every target in the project, or in each project of the workspace (CocoaPods' too),
with xcodebuild -showBuildSettings, and the script phases from the project files. It builds nothing and changes
nothing. Swift packages keep their own settings and aren't checked. It reports:

  current values   every setting it checks, and how many targets have each value
  to change        settings with a value that makes every debug build slower (whole-module compilation,
                   optimization, dSYM files, more than one architecture)
  every build      script phases that run on every build, and why
  differ           settings that differ between targets and make shared modules build more than once
  worth trying     settings that may help, depending on the project (eager linking, scripts in parallel,
                   index data from the command line, explicit modules)

Every finding names the setting as the Build Settings tab shows it and by its key, the value to use, why, and
the section of https://quickfirebuild.com/blog/debug-build-settings/ that explains it.

The checks assume the configuration you build all day (Debug unless you pass --configuration).
Python 3.9 or later, standard library only.

Examples:
  check_settings.py                                   # the workspace or project in this folder
  check_settings.py --workspace MyApp.xcworkspace
  check_settings.py --json > settings.json            # for scripts and coding agents
  check_settings.py -- -skipPackagePluginValidation   # extra xcodebuild arguments after --
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


class Stop(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


JSON_MODE = False


def say(text=""):
    print(text, file=sys.stderr if JSON_MODE else sys.stdout, flush=True)


# --- running xcodebuild -------------------------------------------------------------------------------------

ERROR_LINE = re.compile(r"(^|: )(fatal )?error: ")


def run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def xcodebuild_json(args):
    code, out, err = run(["xcodebuild"] + args)
    if code != 0:
        lines = [l for l in (out + err).splitlines() if ERROR_LINE.search(l)] or (out + err).splitlines()[-20:]
        raise Stop("xcodebuild %s failed:\n  %s" % (" ".join(args), "\n  ".join(lines[:10])), 2)
    starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
    try:
        if not starts:
            raise ValueError
        return json.loads(out[min(starts):])
    except ValueError:
        raise Stop("couldn't read the output of xcodebuild %s" % " ".join(args), 2)


def find_container(args):
    if args.workspace:
        return Path(args.workspace)
    if args.project:
        return Path(args.project)
    here = Path.cwd()
    workspaces = sorted(here.glob("*.xcworkspace"))
    projects = sorted(here.glob("*.xcodeproj"))
    if len(workspaces) == 1:
        return workspaces[0]
    if len(workspaces) > 1:
        raise Stop("more than one workspace here (%s): pass --workspace" % ", ".join(p.name for p in workspaces), 2)
    if len(projects) == 1:
        return projects[0]
    if len(projects) > 1:
        raise Stop("more than one project here (%s): pass --project" % ", ".join(p.name for p in projects), 2)
    raise Stop("no .xcworkspace or .xcodeproj in %s: run this from your project's folder, "
               "or pass --workspace or --project" % here, 2)


def workspace_projects(workspace):
    """The .xcodeproj files a workspace refers to, following its groups."""
    import xml.etree.ElementTree as ET
    data = workspace / "contents.xcworkspacedata"
    if not data.exists():
        raise Stop("%s has no contents.xcworkspacedata" % workspace, 2)
    base = workspace.parent
    found = []

    def resolve(location, folder):
        kind, _, path = location.partition(":")
        if kind == "absolute":
            return Path(path)
        if kind == "group":
            return folder / path
        return base / path  # container: and self:

    def walk(node, folder):
        for child in node:
            location = child.get("location", "")
            if child.tag == "Group":
                walk(child, resolve(location, folder) if location else folder)
            elif child.tag == "FileRef" and location.endswith(".xcodeproj"):
                found.append(resolve(location, folder))

    walk(ET.parse(data).getroot(), base)
    return [p for p in found if p.exists()]


def read_settings(projects, args):
    entries = []
    for project in projects:
        settings = xcodebuild_json(["-project", str(project), "-alltargets", "-configuration", args.configuration,
                                    "-showBuildSettings", "-json"] + args.extra)
        seen = set()
        for e in settings:
            if e["target"] in seen:
                continue  # a target that supports more than one platform is listed once per platform
            seen.add(e["target"])
            e["project"] = project.stem
            e["label"] = e["target"] if len(projects) == 1 else "%s/%s" % (project.stem, e["target"])
            e["path"] = str(project)
            entries.append(e)
    return entries


# --- the checks ---------------------------------------------------------------------------------------------

ARTICLE = "https://quickfirebuild.com/blog/debug-build-settings/"


def is_static(s):
    return s.get("MACH_O_TYPE") == "staticlib"


def is_dynamic_framework(s):
    return s.get("PRODUCT_TYPE") == "com.apple.product-type.framework" and s.get("MACH_O_TYPE") == "mh_dylib"


# Each setting the report shows. "slow" says whether a target's value slows every build of the configuration
# (None: the value is shown, but never reported as a problem by itself).
SETTINGS = [
    {"key": "SWIFT_COMPILATION_MODE", "name": "Compilation Mode", "recommended": "singlefile",
     "shown_as": "Incremental", "anchor": "1-compilation-mode",
     "slow": lambda s: s.get("SWIFT_COMPILATION_MODE") == "wholemodule",
     "why": "Whole Module recompiles every Swift file in the module after any change, in one compiler process. "
            "Keep it for release builds."},
    {"key": "SWIFT_OPTIMIZATION_LEVEL", "name": "Optimization Level (Swift)", "recommended": "-Onone",
     "shown_as": "no optimization", "anchor": "2-optimization-level",
     "slow": lambda s: s.get("SWIFT_OPTIMIZATION_LEVEL", "-Onone") not in ("-Onone", ""),
     "why": "The optimizer adds compile time to every build and makes debugging harder. Turn it on only in "
            "the one target that needs it, or profile a release build."},
    {"key": "GCC_OPTIMIZATION_LEVEL", "name": "Optimization Level (C and Objective-C)", "recommended": "0",
     "shown_as": "no optimization", "anchor": "2-optimization-level",
     "slow": lambda s: s.get("GCC_OPTIMIZATION_LEVEL", "0") not in ("0", ""),
     "why": "The optimizer adds compile time to every build and makes debugging harder."},
    {"key": "DEBUG_INFORMATION_FORMAT", "name": "Debug Information Format", "recommended": "dwarf",
     "shown_as": "DWARF", "anchor": "3-debug-information-format",
     "slow": lambda s: s.get("DEBUG_INFORMATION_FORMAT") == "dwarf-with-dsym" and not is_static(s),
     "why": "DWARF with dSYM File runs an extra step after every link to write the dSYM. Only builds you ship "
            "or hand to testers need one."},
    {"key": "ONLY_ACTIVE_ARCH", "name": "Build Active Architecture Only", "recommended": "YES",
     "shown_as": "Yes", "anchor": "4-build-active-architecture-only",
     "slow": lambda s: s.get("ONLY_ACTIVE_ARCH", "NO") != "YES",
     "why": "With No, every architecture the target supports is compiled, not only the one you run on."},
    {"key": "ENABLE_USER_SCRIPT_SANDBOXING", "name": "User Script Sandboxing", "recommended": "YES",
     "shown_as": "Yes", "anchor": "5-script-phases", "slow": None,
     "why": "Fails the build when a script uses a file it didn't declare, which keeps input and output lists "
            "honest."},
    {"key": "FUSE_BUILD_SCRIPT_PHASES", "name": "Run Build Script Phases in Parallel", "recommended": "YES",
     "shown_as": "Yes", "anchor": "5-script-phases", "slow": None,
     "why": "Lets a target's script phases run in parallel, once every one declares complete inputs and outputs."},
    {"key": "SWIFT_ENABLE_EXPLICIT_MODULES", "name": "Explicitly Built Modules", "recommended": "YES",
     "shown_as": "Yes", "anchor": "6-settings-that-shape-modules", "slow": None,
     "why": "The default for Swift since Xcode 26. If it was turned off to work around an error, try it again."},
    {"key": "COMPILER_INDEX_STORE_ENABLE", "name": "Enable Index-While-Building Functionality",
     "recommended": "Default", "shown_as": "Default (NO on the command line)", "anchor": "7-index-data",
     "slow": None,
     "why": "Leave it for builds in the IDE. Command-line builds (CI, coding agents) can pass "
            "COMPILER_INDEX_STORE_ENABLE=NO, with a build folder of their own."},
    {"key": "EAGER_LINKING", "name": "Eager Linking", "recommended": "YES", "shown_as": "Yes",
     "anchor": "8-eager-linking", "slow": None,
     "why": "For Swift-only dynamic frameworks: the targets that link them can start linking sooner."},
]

# Settings that, per Apple's "Demystify explicitly built modules" (WWDC24), make the same module build once per
# distinct value: preprocessor macros, C language standards and automatic reference counting.
VARIANT_SETTINGS = [
    ("GCC_PREPROCESSOR_DEFINITIONS", "Preprocessor Macros"),
    ("GCC_C_LANGUAGE_STANDARD", "C Language Dialect"),
    ("CLANG_ENABLE_OBJC_ARC", "Objective-C Automatic Reference Counting"),
]


def label(names, limit=4):
    names = sorted(names)
    shown = ", ".join(names[:limit])
    return shown + (" and %d more" % (len(names) - limit) if len(names) > limit else "")


def value_of(e, key):
    return " ".join(e["buildSettings"].get(key, "").split())


def current_values(entries):
    """For each setting, which targets have which value."""
    rows = []
    for spec in SETTINGS:
        by_value = {}
        for e in entries:
            by_value.setdefault(value_of(e, spec["key"]) or "(not set: default)", []).append(e["label"])
        rows.append({"setting": spec["key"], "name": spec["name"],
                     "values": [{"value": v, "targets": sorted(t)}
                                for v, t in sorted(by_value.items(), key=lambda kv: -len(kv[1]))]})
    return rows


def to_change(entries):
    """Values that make every build of the configuration slower."""
    found = []
    for spec in SETTINGS:
        if not spec["slow"]:
            continue
        bad = [e for e in entries if spec["slow"](e["buildSettings"])]
        if bad:
            found.append({"setting": spec["key"], "name": spec["name"],
                          "current": sorted({value_of(e, spec["key"]) or "(not set)" for e in bad}),
                          "recommended": spec["recommended"], "shown_as": spec["shown_as"],
                          "targets": sorted(e["label"] for e in bad), "why": spec["why"],
                          "article": ARTICLE + "#" + spec["anchor"]})
    return found


def differing(entries):
    compiling = [e for e in entries if "aggregate" not in e["buildSettings"].get("PRODUCT_TYPE", "aggregate")]
    found = []
    for key, name in VARIANT_SETTINGS:
        by_value = {}
        for e in compiling:
            by_value.setdefault(value_of(e, key) or "(not set)", []).append(e["label"])
        if len(by_value) > 1:
            found.append({"setting": key, "name": name,
                          "values": [{"value": v, "targets": sorted(t)} for v, t in
                                     sorted(by_value.items(), key=lambda kv: -len(kv[1]))],
                          "why": "Each different value can build the same modules again. Set it once at the "
                                 "project level (or in a shared .xcconfig) unless a target really needs its own.",
                          "article": ARTICLE + "#6-settings-that-shape-modules"})
    return found


def script_phases(entries):
    """Script phases that run on every build, and targets whose scripts could run in parallel."""
    by_project = {}
    for e in entries:
        by_project.setdefault(e["path"], []).append(e)
    every_build, could_fuse, unread = [], [], []
    for project, members in sorted(by_project.items()):
        by_name = {e["target"]: e for e in members}
        code, out, _ = run(["plutil", "-convert", "json", "-o", "-", str(Path(project) / "project.pbxproj")])
        if code != 0:
            unread.append(project)
            continue
        objects = json.loads(out).get("objects", {})
        for obj in objects.values():
            if obj.get("isa") not in ("PBXNativeTarget", "PBXAggregateTarget") or obj.get("name") not in by_name:
                continue
            e = by_name[obj["name"]]
            scripts, analysed = 0, 0
            for phase_id in obj.get("buildPhases", []):
                phase = objects.get(phase_id, {})
                if phase.get("isa") != "PBXShellScriptBuildPhase":
                    continue
                if phase.get("runOnlyForDeploymentPostprocessing") == "1":
                    continue  # "For install builds only"
                scripts += 1
                if phase.get("alwaysOutOfDate") == "1":
                    why = "\"Based on dependency analysis\" is off"
                elif not phase.get("outputPaths") and not phase.get("outputFileListPaths"):
                    why = "it declares no output files"
                elif not phase.get("inputPaths") and not phase.get("inputFileListPaths"):
                    why = "it declares no input files"
                else:
                    analysed += 1
                    continue
                every_build.append({"target": e["label"], "phase": phase.get("name") or "Run Script", "why": why})
            settings = e["buildSettings"]
            if (scripts >= 2 and analysed == scripts and settings.get("FUSE_BUILD_SCRIPT_PHASES", "NO") != "YES"
                    and settings.get("ENABLE_USER_SCRIPT_SANDBOXING", "NO") == "YES"):
                could_fuse.append(e["label"])
    return every_build, could_fuse, unread


def worth_trying(entries, could_fuse, command_line_index):
    found = []
    frameworks = [e["label"] for e in entries
                  if is_dynamic_framework(e["buildSettings"]) and e["buildSettings"].get("EAGER_LINKING") != "YES"]
    if frameworks:
        found.append({"setting": "EAGER_LINKING", "name": "Eager Linking", "recommended": "YES",
                      "targets": sorted(frameworks),
                      "why": "These dynamic frameworks don't use it. If a framework is Swift only, turning it on "
                             "lets the targets that link it start linking sooner.",
                      "article": ARTICLE + "#8-eager-linking"})
    if could_fuse:
        found.append({"setting": "FUSE_BUILD_SCRIPT_PHASES", "name": "Run Build Script Phases in Parallel",
                      "recommended": "YES", "targets": sorted(could_fuse),
                      "why": "Every script phase in these targets declares its inputs and outputs and sandboxing "
                             "is on, so their scripts could run in parallel instead of one at a time.",
                      "article": ARTICLE + "#5-script-phases"})
    no_explicit = [e["label"] for e in entries if e["buildSettings"].get("SWIFT_ENABLE_EXPLICIT_MODULES") == "NO"]
    if no_explicit:
        found.append({"setting": "SWIFT_ENABLE_EXPLICIT_MODULES", "name": "Explicitly Built Modules",
                      "recommended": "YES", "targets": sorted(no_explicit),
                      "why": "Turned off in these targets. It has been the default for Swift since Xcode 26; if "
                             "it was turned off to work around an error, try it again.",
                      "article": ARTICLE + "#6-settings-that-shape-modules"})
    if command_line_index:
        found.append({"setting": "COMPILER_INDEX_STORE_ENABLE", "name": "Enable Index-While-Building Functionality",
                      "recommended": "NO on the command line", "targets": [],
                      "why": "Debug builds also write index data for the editor. Builds from the command line (CI, "
                             "coding agents) can skip it: add COMPILER_INDEX_STORE_ENABLE=NO to the xcodebuild "
                             "command, with a build folder of their own (-derivedDataPath).",
                      "article": ARTICLE + "#7-index-data"})
    return found


# --- report -------------------------------------------------------------------------------------------------

def xcode_version():
    code, out, _ = run(["xcodebuild", "-version"])
    return " ".join(out.split()) if code == 0 else "unknown"


def counts(values):
    return ", ".join("%s: %d" % (v["value"], len(v["targets"])) for v in values)


def print_report(r):
    say("%s, %s configuration: %d target%s in %d project%s (%s)" % (
        r["container"], r["configuration"], len(r["targets"]), "" if len(r["targets"]) == 1 else "s",
        len(r["projects"]), "" if len(r["projects"]) == 1 else "s", r["xcode"]))
    say()
    say("Current values (value: number of targets):")
    for row in r["current"]:
        say("  %-42s %-30s %s" % (row["name"], row["setting"], counts(row["values"])))
    say()
    if r["to_change"]:
        say("To change (each one slows every build):")
        for i, f in enumerate(r["to_change"], 1):
            say("  %d. %s (%s) is %s in %s." % (i, f["name"], f["setting"], " / ".join(f["current"]),
                                                label(f["targets"])))
            say("     Set it to %s (%s). %s" % (f["shown_as"], f["recommended"], f["why"]))
            say("     More: %s" % f["article"])
    else:
        say("To change: nothing. Compilation mode, optimization, debug information and architectures are set for "
            "fast builds.")
    say()
    if r["every_build"]:
        say("Script phases that run on every build:")
        for p in r["every_build"]:
            say("  %s > %s: %s." % (p["target"], p["phase"], p["why"]))
        say("     Give each one its input and output files, or move it out of the builds you make all day.")
        say("     More: %s#5-script-phases" % ARTICLE)
    else:
        say("Script phases that run on every build: none.")
    for project in r["unreadable_projects"]:
        say("  (couldn't read the script phases of %s)" % project)
    say()
    if r["differ"]:
        say("Settings that differ between targets (each different value can build the same modules again):")
        for d in r["differ"]:
            say("  %s (%s): %d values" % (d["name"], d["setting"], len(d["values"])))
            for v in d["values"]:
                say("    %s: %s" % (v["value"], label(v["targets"], 3)))
        say("     Set them once at the project level (or in a shared .xcconfig) unless a target really needs its "
            "own.")
        say("     More: %s#6-settings-that-shape-modules" % ARTICLE)
        say()
    if r["worth_trying"]:
        say("Worth trying (measure before and after):")
        for t in r["worth_trying"]:
            say("  %s (%s): %s%s" % (t["name"], t["setting"], t["why"],
                                     (" Targets: %s." % label(t["targets"])) if t["targets"] else ""))
        say()
    say("Change one setting at a time, in the Debug configuration only, and time the same build before and after")
    say("(time-build.sh or build_baseline.py). --json prints this report for scripts and coding agents.")


def parse_args(argv):
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="\n\n".join(__doc__.split("\n\n")[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--workspace", help="the .xcworkspace (detected when left out)")
    parser.add_argument("--project", help="the .xcodeproj (detected when left out)")
    parser.add_argument("--configuration", default="Debug", help="build configuration (default: Debug)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    args.extra = extra
    return args


def main(argv):
    global JSON_MODE
    args = parse_args(argv)
    JSON_MODE = args.json
    container = find_container(args)
    projects = workspace_projects(container) if container.suffix == ".xcworkspace" else [container]
    if not projects:
        raise Stop("%s refers to no .xcodeproj" % container, 2)
    say("Reading the build settings of %d project%s..." % (len(projects), "" if len(projects) == 1 else "s"))
    entries = read_settings(projects, args)
    every_build, could_fuse, unread = script_phases(entries)
    report = {
        "container": container.name,
        "projects": [str(p) for p in projects],
        "configuration": args.configuration,
        "xcode": xcode_version(),
        "article": ARTICLE,
        "targets": sorted(e["label"] for e in entries),
        "current": current_values(entries),
        "to_change": to_change(entries),
        "every_build": every_build,
        "unreadable_projects": unread,
        "differ": differing(entries),
        "worth_trying": worth_trying(entries, could_fuse, command_line_index=not (
            any("COMPILER_INDEX_STORE_ENABLE=NO" in a for a in args.extra)
            or all(e["buildSettings"].get("COMPILER_INDEX_STORE_ENABLE") == "NO" for e in entries))),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        say()
        print_report(report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Stop as stop:
        print("check_settings.py: %s" % stop, file=sys.stderr)
        sys.exit(stop.code)
    except KeyboardInterrupt:
        sys.exit(130)
