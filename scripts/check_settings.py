#!/usr/bin/env python3
"""Checks the build settings that decide how long a debug build takes, for every target in your project.

It reads the settings of every target in the project, or in each project of the workspace (CocoaPods' too),
with xcodebuild -showBuildSettings, and the script phases from the project files. It builds nothing and changes
nothing. Swift packages keep their own settings and aren't checked. It reports:

  worth changing   settings with a value that makes every debug build slower (whole-module compilation,
                   optimization, dSYM files, more than one architecture)
  every build      script phases that run on every build, and why
  differ           settings that differ between targets and make shared modules build more than once
  worth trying     settings that may help, depending on the project (eager linking, index data from the
                   command line, explicit modules, script sandboxing)

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

def label(names, limit=4):
    names = sorted(names)
    shown = ", ".join(names[:limit])
    return shown + (" and %d more" % (len(names) - limit) if len(names) > limit else "")


def targets_where(entries, test):
    return [e["label"] for e in entries if test(e["buildSettings"])]


def worth_changing(entries):
    """Values that make every build of the configuration slower."""
    rules = [
        ("SWIFT_COMPILATION_MODE", "Compilation Mode is Whole Module",
         lambda s: s.get("SWIFT_COMPILATION_MODE") == "wholemodule",
         "set it to Incremental (singlefile): Whole Module recompiles every Swift file in the module on every change"),
        ("SWIFT_OPTIMIZATION_LEVEL", "Swift optimization is on",
         lambda s: s.get("SWIFT_OPTIMIZATION_LEVEL", "-Onone") not in ("-Onone", ""),
         "set Optimization Level to None (-Onone): the optimizer adds compile time to every build"),
        ("GCC_OPTIMIZATION_LEVEL", "C and Objective-C optimization is on",
         lambda s: s.get("GCC_OPTIMIZATION_LEVEL", "0") not in ("0", ""),
         "set Optimization Level to None (0)"),
        ("DEBUG_INFORMATION_FORMAT", "Debug Information Format is DWARF with dSYM File",
         lambda s: s.get("DEBUG_INFORMATION_FORMAT") == "dwarf-with-dsym" and s.get("MACH_O_TYPE") != "staticlib",
         "set it to DWARF: a dSYM is generated after every link, and only builds you ship or hand to testers "
         "need one"),
        ("ONLY_ACTIVE_ARCH", "Build Active Architecture Only is off",
         lambda s: s.get("ONLY_ACTIVE_ARCH", "NO") != "YES",
         "set it to Yes: otherwise every architecture the target supports is compiled"),
    ]
    found = []
    for key, what, test, fix in rules:
        targets = targets_where(entries, test)
        if targets:
            values = sorted({e["buildSettings"].get(key, "") for e in entries if e["label"] in targets})
            found.append({"setting": key, "problem": what, "fix": fix, "targets": sorted(targets),
                          "values": values})
    return found


# Settings that, per Apple's "Demystify explicitly built modules" (WWDC24), make the same module build once per
# distinct value: preprocessor macros, C language standards and automatic reference counting.
VARIANT_SETTINGS = [
    "GCC_PREPROCESSOR_DEFINITIONS",
    "GCC_C_LANGUAGE_STANDARD",
    "CLANG_ENABLE_OBJC_ARC",
]


def differing(entries):
    compiling = [e for e in entries
                 if e["buildSettings"].get("PRODUCT_TYPE", "") and "aggregate" not in e["buildSettings"].get(
                     "PRODUCT_TYPE", "")]
    found = []
    for key in VARIANT_SETTINGS:
        by_value = {}
        for e in compiling:
            value = " ".join(e["buildSettings"].get(key, "").split())
            by_value.setdefault(value, []).append(e["label"])
        if len(by_value) > 1:
            found.append({"setting": key,
                          "values": [{"value": v, "targets": sorted(t)} for v, t in
                                     sorted(by_value.items(), key=lambda kv: -len(kv[1]))]})
    return found


def worth_trying(entries, command_line_index):
    found = []
    frameworks = targets_where(entries, lambda s: s.get("PRODUCT_TYPE") == "com.apple.product-type.framework"
                               and s.get("MACH_O_TYPE") == "mh_dylib" and s.get("EAGER_LINKING", "NO") != "YES")
    if frameworks:
        found.append({"setting": "EAGER_LINKING", "targets": sorted(frameworks),
                      "note": "Eager Linking is off for these dynamic frameworks. If a framework is Swift only, "
                              "turning it on lets the targets that link it start linking sooner."})
    no_explicit = targets_where(entries, lambda s: s.get("SWIFT_ENABLE_EXPLICIT_MODULES", "YES") == "NO")
    if no_explicit:
        found.append({"setting": "SWIFT_ENABLE_EXPLICIT_MODULES", "targets": sorted(no_explicit),
                      "note": "Explicitly Built Modules is turned off. It has been the default for Swift since "
                              "Xcode 26; if it was turned off to work around an error, try it again."})
    if command_line_index:
        found.append({"setting": "COMPILER_INDEX_STORE_ENABLE", "targets": [],
                      "note": "Debug builds also write index data, for code navigation in the editor. Builds from "
                              "the command line (CI, coding agents) can skip it: add COMPILER_INDEX_STORE_ENABLE=NO "
                              "to the xcodebuild command, with a build folder of their own (-derivedDataPath)."})
    return found


def script_phases(entries):
    """Script phases that run on every build, read from the project files."""
    by_project = {}
    for e in entries:
        by_project.setdefault(e["path"], []).append(e)
    found, unread = [], []
    for project, members in sorted(by_project.items()):
        labels = {e["target"]: e["label"] for e in members}
        sandboxed = {e["target"]: e["buildSettings"].get("ENABLE_USER_SCRIPT_SANDBOXING", "NO") == "YES"
                     for e in members}
        code, out, _ = run(["plutil", "-convert", "json", "-o", "-", str(Path(project) / "project.pbxproj")])
        if code != 0:
            unread.append(project)
            continue
        objects = json.loads(out).get("objects", {})
        for obj in objects.values():
            if obj.get("isa") not in ("PBXNativeTarget", "PBXAggregateTarget") or obj.get("name") not in labels:
                continue
            for phase_id in obj.get("buildPhases", []):
                phase = objects.get(phase_id, {})
                if phase.get("isa") != "PBXShellScriptBuildPhase":
                    continue
                if phase.get("runOnlyForDeploymentPostprocessing") == "1":
                    continue  # "For install builds only"
                if phase.get("alwaysOutOfDate") == "1":
                    why = "\"Based on dependency analysis\" is off"
                elif not phase.get("outputPaths") and not phase.get("outputFileListPaths"):
                    why = "it declares no output files"
                else:
                    continue
                found.append({"target": labels[obj["name"]], "phase": phase.get("name") or "Run Script",
                              "why": why, "sandboxed": sandboxed.get(obj["name"], False)})
    return found, unread


# --- report -------------------------------------------------------------------------------------------------

def xcode_version():
    code, out, _ = run(["xcodebuild", "-version"])
    return " ".join(out.split()) if code == 0 else "unknown"


def print_report(r):
    say("%s, %s configuration: %d target%s in %d project%s (%s)" % (
        r["container"], r["configuration"], len(r["targets"]), "" if len(r["targets"]) == 1 else "s",
        len(r["projects"]), "" if len(r["projects"]) == 1 else "s", r["xcode"]))
    say()
    if r["worth_changing"]:
        say("Worth changing (each one slows every build):")
        for f in r["worth_changing"]:
            say("  %s in %s." % (f["problem"], label(f["targets"])))
            say("    Fix: %s. (%s)" % (f["fix"], f["setting"]))
    else:
        say("Worth changing: nothing. Compilation mode, optimization, debug information and architectures "
            "are set for fast builds.")
    say()
    if r["every_build"]:
        say("Script phases that run on every build:")
        for p in r["every_build"]:
            say("  %s > %s: %s." % (p["target"], p["phase"], p["why"]))
        say("    Fix: give each one its input and output files, or move it out of the builds you make all day.")
        if any(not p["sandboxed"] for p in r["every_build"]):
            say("    User Script Sandboxing (ENABLE_USER_SCRIPT_SANDBOXING) fails the build when a script uses a "
                "file it didn't declare,")
            say("    which keeps the lists honest.")
    else:
        say("Script phases that run on every build: none.")
    for project in r["unreadable_projects"]:
        say("  (couldn't read the script phases of %s)" % project)
    say()
    if r["differ"]:
        say("Settings that differ between targets (each different value can build the same modules again):")
        for d in r["differ"]:
            say("  %s: %d values" % (d["setting"], len(d["values"])))
            for v in d["values"]:
                say("    %s: %s" % (v["value"] or "(not set)", label(v["targets"], 3)))
        say("    Fix: set them once at the project level (or in a shared .xcconfig) unless a target really needs "
            "its own.")
        say()
    if r["worth_trying"]:
        say("Worth trying (measure before and after):")
        for t in r["worth_trying"]:
            say("  %s%s" % (t["note"], (" Targets: %s." % label(t["targets"])) if t["targets"] else ""))
    say()
    say("Change one setting at a time and time the same build before and after (time-build.sh or build_baseline.py).")


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
    every_build, unread = script_phases(entries)
    report = {
        "container": container.name,
        "projects": [str(p) for p in projects],
        "configuration": args.configuration,
        "xcode": xcode_version(),
        "targets": sorted(e["label"] for e in entries),
        "worth_changing": worth_changing(entries),
        "every_build": every_build,
        "unreadable_projects": unread,
        "differ": differing(entries),
        "worth_trying": worth_trying(entries, command_line_index=not (
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
