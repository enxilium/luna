"""Build build/Luna.app: Luna as one Mac app. Opening it starts the notch, wake word and voice, and the dashboard together.

It's a tiny native executable that runs this checkout's Python (`.venv`, synced first) inside itself, so Luna is one
process with its own identity for macOS privacy prompts (microphone, calendar, location, Messages, Full Disk Access)
instead of the terminal's, and no Dock icon (an agent app): Luna stays in the background until you say "Luna", point at
the notch or press the dashboard's shortcut. Quitting Luna (⌘Q in the dashboard, or Quit in Activity Monitor) stops all
of it. Nothing is installed: the bundle stays in build/; copy it to Applications if you like, but it keeps running this
checkout.

It's signed with your certificate when you have one (a Developer ID, else an Apple Development certificate from Xcode;
LUNA_SIGN_IDENTITY picks one by name or hash), so macOS keeps Luna's permissions across rebuilds. Without one it's
ad-hoc signed, and every rebuild is a new app that asks again.

    uv run python -m scripts.build_app          # then: open build/Luna.app
"""

import os
import platform
import plistlib
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "build" / "Luna.app"
BUNDLE_ID = "dev.luna"
PYTHON = ROOT / ".venv" / "bin" / "python"
MACOS = ".".join(platform.mac_ver()[0].split(".")[:2])  # the Python library is built for this macOS

# Python runs inside the bundle's own process (embedded, as `.venv/bin/python main.py` would run), so macOS sees one app:
# privacy prompts, Quit (logout, Activity Monitor) and the process list all name Luna.
LAUNCHER = r"""
#include <Python.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int failed(PyStatus status, PyConfig *config) {
    PyConfig_Clear(config);
    Py_ExitStatusException(status);
    return 1;
}

int main(int argc, char **argv) {
    char log_dir[1024], log_path[1100];
    snprintf(log_dir, sizeof log_dir, "%s/Library/Logs/Luna", getenv("HOME"));
    mkdir(log_dir, 0700);
    snprintf(log_path, sizeof log_path, "%s/luna.log", log_dir);
    int log = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (log >= 0) { dup2(log, 1); dup2(log, 2); close(log); }
    if (chdir(ROOT) != 0) return 1;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.parse_argv = 0;
    /* The venv's interpreter path makes Python find .venv/pyvenv.cfg, its packages and its base install. Arguments pass
       through (`open build/Luna.app --args --demo`), minus the process serial number older macOS adds. */
    char *args[64] = {ROOT "/main.py"};  /* sys.argv, as `python main.py …` would set it */
    int count = 1;
    for (int i = 1; i < argc && count < 63; i++) if (strncmp(argv[i], "-psn_", 5) != 0) args[count++] = argv[i];
    PyStatus status = PyConfig_SetBytesString(&config, &config.program_name, PYTHON);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesString(&config, &config.executable, PYTHON);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesString(&config, &config.run_filename, ROOT "/main.py");
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesArgv(&config, count, args);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) return failed(status, &config);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
"""

REASONS = {
    "NSMicrophoneUsageDescription": "Luna listens for its name on this Mac and hears you when you talk to it.",
    "NSCalendarsFullAccessUsageDescription": "Luna shows your events on the dashboard's timeline. It only reads your calendars.",
    "NSCalendarsUsageDescription": "Luna shows your events on the dashboard's timeline. It only reads your calendars.",
    "NSLocationUsageDescription": "Luna shows the weather where you are. Only your town-level location is used.",
    "NSLocationWhenInUseUsageDescription": "Luna shows the weather where you are. Only your town-level location is used.",
    "NSAppleEventsUsageDescription": "Luna sends the iMessages you write in its inbox through Messages.",
}


def c_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def icon(resources):
    """The Diamond Ring the dashboard draws for itself, as the bundle's icon."""
    from src.dashboard.window import dock_icon
    import AppKit as AK

    with tempfile.TemporaryDirectory() as scratch:
        iconset = Path(scratch) / "Luna.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                rep = AK.NSBitmapImageRep.imageRepWithData_(dock_icon(pixels).TIFFRepresentation())
                png = rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, {})
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                png.writeToFile_atomically_(str(iconset / name), True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Luna.icns")], check=True)


def signing_identity():
    """(hash, name) of the certificate to sign with, or None for ad-hoc."""
    found = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"], capture_output=True, text=True).stdout
    identities = re.findall(r'^\s*\d+\) ([0-9A-F]{40}) "(.+)"$', found, re.M)
    wanted = os.environ.get("LUNA_SIGN_IDENTITY")
    if wanted:
        match = next((i for i in identities if wanted in (i[0], i[1]) or wanted in i[1]), None)
        if match is None:
            raise SystemExit(f"No valid signing identity matches LUNA_SIGN_IDENTITY={wanted!r}.")
        return match
    for kind in ("Developer ID Application:", "Apple Development:"):
        match = next((i for i in identities if i[1].startswith(kind)), None)
        if match:
            return match
    return None


def build():
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv isn't on PATH.")
    subprocess.run([uv, "sync", "--project", str(ROOT)], check=True)  # the launcher runs .venv's Python directly
    if APP.exists():
        shutil.rmtree(APP)
    macos, resources = APP / "Contents" / "MacOS", APP / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "launcher.c"
        source.write_text(LAUNCHER)
        paths = subprocess.run([str(PYTHON), "-c", "import sysconfig as s; print(s.get_config_var('INCLUDEPY')); "
                                "print(s.get_config_var('LIBDIR')); print(s.get_config_var('VERSION'))"],
                               check=True, capture_output=True, text=True).stdout.split()
        include, libdir, version = paths
        subprocess.run(["xcrun", "clang", "-O2", "-Wall", f"-DROOT={c_string(ROOT)}", f"-DPYTHON={c_string(PYTHON)}",
                        f"-I{include}", f"-L{libdir}", f"-lpython{version}", f"-Wl,-rpath,{libdir}",
                        f"-mmacosx-version-min={MACOS}", "-o", str(macos / "luna"), str(source)], check=True)
    icon(resources)
    plist = {
        "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Luna", "CFBundleDisplayName": "Luna",
        "CFBundleExecutable": "luna", "CFBundleIconFile": "Luna", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0", "CFBundleVersion": "1", "LSMinimumSystemVersion": MACOS,
        **REASONS,
    }
    with open(APP / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump(plist, handle)
    # macOS remembers Luna's permissions by its signature: a certificate keeps them across rebuilds, ad-hoc doesn't.
    identity = signing_identity()
    subprocess.run(["codesign", "--force", "--sign", identity[0] if identity else "-", "--identifier", BUNDLE_ID, str(APP)],
                   check=True)
    signed = f"signed as {identity[1]}" if identity else "ad-hoc signed (macOS will ask for permissions again after each rebuild)"
    print(f"Built {APP.relative_to(ROOT)}, {signed}. Open it with: open {APP.relative_to(ROOT)}")


if __name__ == "__main__":
    build()
