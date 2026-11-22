"""Package Luna as a self-contained Mac app and a DMG to install it from, for you and for friends.

Unlike scripts/build_app.py (which runs this checkout, for development), the packaged Luna.app carries everything it
runs: its own Python with every locked package, Luna's code, uv (for the memory service's and Canvas's own Python),
Ollama (memory's model server), Node and the prebuilt Spotify server. First-run setup in the app does the rest: the
Gemini key, the wake word model, and (if you like) memory's model, Google, Spotify and Canvas.

    uv run python -m scripts.package_app                                 # build/Luna-<version>.dmg
    uv run python -m scripts.package_app --with-google --with-spotify    # friends sign in through your OAuth apps
    uv run python -m scripts.package_app --with-model                    # memory's ~18 GB model inside the app

--with-google copies your Google OAuth Desktop app client (the one scripts/setup_google.py imported) into the app, and
--with-spotify your SPOTIFY_* app from .env. Neither carries a real secret: a Desktop app client's secret is public by
Google's design, and Spotify signs in with PKCE and a client ID alone. While those apps are in testing mode, add the
people you share Luna with as test users (Google Cloud → OAuth consent screen; Spotify Developer Dashboard → User
Management).

Signing: a Developer ID Application certificate (paid Apple Developer Program) gives an app friends open without
warnings, once notarized (--notarize PROFILE, a `xcrun notarytool store-credentials` profile). An Apple Development
certificate works on your Mac; on a friend's Mac it opens after System Settings → Privacy & Security → Open Anyway.
"""

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from scripts.build_app import BUNDLE_ID, REASONS, c_string, icon, signing_identity

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
CACHE = BUILD / "package-cache"
APP = BUILD / "package" / "Luna.app"
PYTHON_VERSION = "3.14.5"  # uv's portable CPython (python-build-standalone)
UV_VERSION = "0.11.14"
OLLAMA_VERSION = "0.40.16"
MINIMUM_MACOS = "14.0"
APP_FILES = ["main.py", "src", "scripts/memory_service.py", "scripts/patches"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", ".DS_Store", "tests")

ENTITLEMENTS = {
    # Python, PyObjC and Node build code at run time; Python loads packages' own libraries.
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.cs.allow-unsigned-executable-memory": True,
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.device.audio-input": True,
    "com.apple.security.personal-information.calendars": True,
    "com.apple.security.personal-information.location": True,
    "com.apple.security.automation.apple-events": True,
}

# The packaged launcher runs Python from inside the bundle: its own interpreter, its packages and Luna's code, found
# relative to the executable, never writing bytecode into the (signed) bundle.
LAUNCHER = r"""
#include <Python.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
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

static PyStatus append(PyConfig *config, const char *path) {
    wchar_t *wide = Py_DecodeLocale(path, NULL);
    PyStatus status = PyWideStringList_Append(&config->module_search_paths, wide);
    PyMem_RawFree(wide);
    return status;
}

int main(int argc, char **argv) {
    char log_dir[PATH_MAX], log_path[PATH_MAX];
    snprintf(log_dir, sizeof log_dir, "%s/Library/Logs/Luna", getenv("HOME"));
    mkdir(log_dir, 0700);
    snprintf(log_path, sizeof log_path, "%s/luna.log", log_dir);
    int log = open(log_path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (log >= 0) { dup2(log, 1); dup2(log, 2); close(log); }

    char exe[PATH_MAX], real[PATH_MAX], near[PATH_MAX], resources[PATH_MAX], app[PATH_MAX], home[PATH_MAX],
         python[PATH_MAX], packages[PATH_MAX], main_py[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) { fprintf(stderr, "Luna: can't find itself\n"); return 1; }
    snprintf(near, sizeof near, "%s/../Resources", dirname(real));
    if (!realpath(near, resources)) { fprintf(stderr, "Luna: can't find its resources (%s)\n", near); return 1; }
    snprintf(app, sizeof app, "%s/app", resources);
    snprintf(home, sizeof home, "%s/python", resources);
    snprintf(python, sizeof python, "%s/python/bin/python3", resources);
    snprintf(packages, sizeof packages, "%s/site-packages", resources);
    snprintf(main_py, sizeof main_py, "%s/main.py", app);
    if (chdir(app) != 0) { fprintf(stderr, "Luna: can't open its code (%s)\n", app); return 1; }

    /* UTF-8 mode: opened from Finder there is no LANG, and the isolated config would otherwise leave files and pipes
       in ASCII (the wake word's tokens.txt then fails to read and voice takes Luna down). */
    PyPreConfig preconfig;
    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    PyStatus pre = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(pre)) Py_ExitStatusException(pre);

    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.write_bytecode = 0;
    config.install_signal_handlers = 1;
    config.user_site_directory = 0;
    char *args[64] = {main_py};
    int count = 1;
    for (int i = 1; i < argc && count < 63; i++) if (strncmp(argv[i], "-psn_", 5) != 0) args[count++] = argv[i];
    PyStatus status = PyConfig_SetBytesString(&config, &config.home, home);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesString(&config, &config.executable, python);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesString(&config, &config.run_filename, main_py);
    if (PyStatus_Exception(status)) return failed(status, &config);
    status = PyConfig_SetBytesArgv(&config, count, args);
    if (PyStatus_Exception(status)) return failed(status, &config);
    /* The whole search path, in order: the standard library and its compiled modules, Luna's packages, Luna's code. */
    char stdlib[PATH_MAX], dynload[PATH_MAX];
    snprintf(stdlib, sizeof stdlib, "%s/lib/python3.14", home);
    snprintf(dynload, sizeof dynload, "%s/lib/python3.14/lib-dynload", home);
    config.module_search_paths_set = 1;
    const char *search[] = {stdlib, dynload, packages, app};
    for (int i = 0; i < 4; i++) {
        status = append(&config, search[i]);
        if (PyStatus_Exception(status)) return failed(status, &config);
    }
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) return failed(status, &config);
    PyConfig_Clear(&config);
    return Py_RunMain();
}
"""


def step(text):
    print(f"• {text}", flush=True)


def fetch(url, destination, sha256=None):
    """Download once into the cache; check the published SHA-256 when there is one."""
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
        partial.rename(destination)
    if sha256 and hashlib.sha256(destination.read_bytes()).hexdigest() != sha256:
        destination.unlink()
        raise SystemExit(f"Checksum mismatch for {destination.name}; the download was discarded.")
    return destination


def published_sha256(url, name):
    text = urllib.request.urlopen(url, timeout=60).read().decode()
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[-1].lstrip("*").split("/")[-1] == name:
            return parts[0]
    raise SystemExit(f"No published checksum for {name}.")


def python_runtime(resources):
    step(f"Python {PYTHON_VERSION}")
    subprocess.run(["uv", "python", "install", PYTHON_VERSION, "--install-dir", str(CACHE / "pythons")], check=True,
                   capture_output=True)
    source = CACHE / "pythons" / f"cpython-{PYTHON_VERSION}-macos-aarch64-none"
    shutil.copytree(source, resources / "python", symlinks=True, ignore=shutil.ignore_patterns("BUILD", "__pycache__"))
    # The launcher links libpython from inside the bundle.
    subprocess.run(["install_name_tool", "-id", "@rpath/libpython3.14.dylib",
                    str(resources / "python/lib/libpython3.14.dylib")], check=True)


def packages(resources):
    step("Luna's Python packages")
    with tempfile.TemporaryDirectory() as scratch:
        requirements = Path(scratch) / "requirements.txt"
        subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "-o", str(requirements)],
                       cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["uv", "pip", "install", "--quiet", "--python", str(resources / "python/bin/python3"),
                        "--target", str(resources / "site-packages"), "-r", str(requirements)], check=True)


def app_code(resources):
    step("Luna's code")
    target = resources / "app"
    for name in APP_FILES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=SKIP)
        else:
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name)
    # The dashboard editor's source and build tools aren't needed: its bundle is in web/vendor.
    shutil.rmtree(target / "src/dashboard/editor", ignore_errors=True)


def tools(resources):
    from src.install.spotify import NODE_VERSION, build

    step(f"uv {UV_VERSION}")
    name = "uv-aarch64-apple-darwin.tar.gz"
    base = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
    archive = fetch(base + name, CACHE / f"uv-{UV_VERSION}.tar.gz",
                    urllib.request.urlopen(base + name + ".sha256", timeout=60).read().decode().split()[0])
    (resources / "bin").mkdir()
    with tarfile.open(archive) as bundle:
        member = next(m for m in bundle.getmembers() if m.name.endswith("/uv"))
        with bundle.extractfile(member) as source, (resources / "bin/uv").open("wb") as output:
            shutil.copyfileobj(source, output)
    (resources / "bin/uv").chmod(0o755)

    step(f"Ollama {OLLAMA_VERSION}")
    name = "ollama-darwin.tgz"
    base = f"https://github.com/ollama/ollama/releases/download/v{OLLAMA_VERSION}/"
    archive = fetch(base + name, CACHE / f"ollama-{OLLAMA_VERSION}.tgz", published_sha256(base + "sha256sum.txt", name))
    (resources / "ollama").mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(resources / "ollama", filter="data")

    step(f"Node {NODE_VERSION}")
    name = f"node-v{NODE_VERSION}-darwin-arm64.tar.xz"
    base = f"https://nodejs.org/dist/v{NODE_VERSION}/"
    archive = fetch(base + name, CACHE / name, published_sha256(base + "SHASUMS256.txt", name))
    with tempfile.TemporaryDirectory() as scratch:
        with tarfile.open(archive) as bundle:
            bundle.extractall(scratch, filter="data")
        shutil.copytree(Path(scratch) / name.removesuffix(".tar.xz"), resources / "node", symlinks=True,
                        ignore=shutil.ignore_patterns("include", "share"))

    step("The Spotify server (pinned and patched)")
    server = CACHE / "spotify-server"
    build(server, resources / "node/bin/node")
    shutil.copytree(server, resources / "servers/spotify", symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "spotify-config.json", "src", "*.md"))
    # Only what it runs on: the build tools (TypeScript, its native compiler, the linter) stay out of the app.
    node = resources / "node/bin"
    subprocess.run([str(node / "npm"), "prune", "--omit=dev", "--no-audit", "--no-fund"], cwd=resources / "servers/spotify",
                   env=os.environ | {"PATH": f"{node}{os.pathsep}{os.environ.get('PATH', '')}"}, check=True,
                   capture_output=True)


def credentials(resources, google, spotify):
    from src import resources as dev
    from src.paths import DEFAULT_HOME

    folder = resources / "credentials"
    if google:
        source = DEFAULT_HOME / "google-oauth-client.json"
        if not source.is_file():
            raise SystemExit("--with-google: import your Google client first (scripts/setup_google.py).")
        folder.mkdir(exist_ok=True)
        shutil.copy2(source, folder / "google-oauth-client.json")
        step("Your Google OAuth client (friends sign in through it)")
    if spotify:
        app = dev.spotify_app()
        if not app:
            raise SystemExit("--with-spotify: add SPOTIFY_CLIENT_ID and SPOTIFY_REDIRECT_URI to .env.")
        folder.mkdir(exist_ok=True)
        (folder / "spotify.json").write_text(json.dumps(app))
        step("Your Spotify app (friends sign in through it)")
    for path in folder.glob("*") if folder.exists() else ():
        path.chmod(0o644)


def model(resources):
    """Memory's model from your Ollama (pull it first), so setup copies it instead of downloading ~18 GB."""
    from src.install.memory import OLLAMA_MODELS, model_files
    from src.paths import MEMORY_MODEL

    files = model_files(OLLAMA_MODELS)
    if not files:
        raise SystemExit(f"--with-model: {MEMORY_MODEL} isn't in ~/.ollama; run `ollama pull {MEMORY_MODEL}` first.")
    for relative in files:
        destination = resources / "models/ollama" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(OLLAMA_MODELS / relative, destination)
        destination.chmod(0o644)
    step(f"Memory's model ({MEMORY_MODEL})")


def precompile(resources):
    step("Compiling Python ahead of time")
    python = resources / "python/bin/python3"
    subprocess.run([str(python), "-m", "compileall", "-q", "-j0", str(resources / "app"), str(resources / "site-packages"),
                    str(resources / "python/lib")], check=False, capture_output=True)


def launcher(macos, resources):
    step("The launcher")
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "launcher.c"
        source.write_text(LAUNCHER)
        subprocess.run(["xcrun", "clang", "-O2", "-Wall", f"-I{resources}/python/include/python3.14",
                        f"-L{resources}/python/lib", "-lpython3.14", "-Wl,-rpath,@executable_path/../Resources/python/lib",
                        f"-mmacosx-version-min={MINIMUM_MACOS}", "-o", str(macos / "luna"), str(source)], check=True)


def machos(root):
    """Every Mach-O file under root, deepest first (nested code is signed before what contains it)."""
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                magic = handle.read(4)
            if magic in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xfe\xed\xfa\xcf"):
                found.append(path)
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def sign(identity, entitlements):
    name = identity[1] if identity else "ad hoc"
    step(f"Signing ({name})")
    # AppleDouble files ("._name", macOS metadata stored as files by some archives, e.g. Ollama's) would be sealed into
    # the signature, then folded back into metadata by any copy (into the DMG, to Applications), breaking the seal.
    for stray in APP.rglob("._*"):
        stray.unlink()
    hardened = bool(identity and identity[1].startswith("Developer ID Application:"))
    base = ["codesign", "--force", "--sign", identity[0] if identity else "-"]
    if hardened:
        base += ["--options", "runtime", "--timestamp", "--entitlements", str(entitlements)]
    for path in machos(APP / "Contents/Resources"):
        subprocess.run(base + [str(path)], check=True, capture_output=True)
    subprocess.run(base + ["--identifier", BUNDLE_ID, str(APP)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(APP)], check=True)
    return hardened


def dmg(version, identity):
    step("The disk image")
    image = APP.parent / f"Luna-{version}.dmg"
    image.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        subprocess.run(["ditto", str(APP), str(Path(staging) / "Luna.app")], check=True)
        (Path(staging) / "Applications").symlink_to("/Applications")
        subprocess.run(["hdiutil", "create", "-volname", "Luna", "-srcfolder", staging, "-ov", "-format", "UDZO",
                        str(image)], check=True, capture_output=True)
    if identity:
        subprocess.run(["codesign", "--force", "--sign", identity[0], str(image)], check=True)
    return image


def notarize(image, profile):
    step("Notarizing with Apple (a few minutes)")
    subprocess.run(["xcrun", "notarytool", "submit", str(image), "--keychain-profile", profile, "--wait"], check=True)
    subprocess.run(["xcrun", "stapler", "staple", str(image)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-google", action="store_true", help="Include your Google OAuth client for friends' sign-in")
    parser.add_argument("--with-spotify", action="store_true", help="Include your Spotify app (from .env) for friends' sign-in")
    parser.add_argument("--with-model", action="store_true", help="Include memory's model (~18 GB) instead of downloading it")
    parser.add_argument("--notarize", metavar="PROFILE", help="Notarize with this notarytool keychain profile (Developer ID only)")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if APP.exists():
        shutil.rmtree(APP)
    macos, resources = APP / "Contents/MacOS", APP / "Contents/Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    python_runtime(resources)
    packages(resources)
    app_code(resources)
    tools(resources)
    credentials(resources, args.with_google, args.with_spotify)
    if args.with_model:
        model(resources)
    precompile(resources)
    launcher(macos, resources)
    icon(resources)
    plist = {
        "CFBundleIdentifier": BUNDLE_ID, "CFBundleName": "Luna", "CFBundleDisplayName": "Luna",
        "CFBundleExecutable": "luna", "CFBundleIconFile": "Luna", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": version, "CFBundleVersion": version, "LSMinimumSystemVersion": MINIMUM_MACOS,
        "LSArchitecturePriority": ["arm64"], **REASONS,
    }
    with open(APP / "Contents/Info.plist", "wb") as handle:
        plistlib.dump(plist, handle)
    identity = signing_identity()
    with tempfile.NamedTemporaryFile("wb", suffix=".plist", delete=False) as handle:
        plistlib.dump(ENTITLEMENTS, handle)
    try:
        hardened = sign(identity, Path(handle.name))
    finally:
        os.unlink(handle.name)
    image = dmg(version, identity)
    if args.notarize:
        if not hardened:
            raise SystemExit("Notarizing needs a Developer ID Application certificate.")
        notarize(image, args.notarize)
    size = sum(p.stat().st_size for p in APP.rglob("*") if p.is_file() and not p.is_symlink()) / 1e9
    print(f"\nBuilt {APP.relative_to(ROOT)} ({size:.1f} GB) and {image.relative_to(ROOT)} "
          f"({image.stat().st_size / 1e9:.1f} GB).")
    if not identity:
        print("It's ad-hoc signed: friends' Macs will refuse it. Create an Apple Development certificate in Xcode.")
    elif not hardened:
        print("Signed with an Apple Development certificate: on a friend's Mac it opens after System Settings → "
              "Privacy & Security → Open Anyway (a Developer ID and --notarize remove that step).")


if __name__ == "__main__":
    main()
