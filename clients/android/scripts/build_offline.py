#!/usr/bin/env python3
"""Fail-closed, no-download Android debug build using an already prepared SDK.

This uses installed Gradle with --offline; it never bootstraps a wrapper,
downloads SDK components, installs on a device, or contacts a REALM server.
Gradle plugins are executable code: for a *network-isolated* proof, run inside
an OS-level network-denied environment as well.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
APK = ROOT / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"


class PreflightError(Exception):
    pass


def checked_version(executable: str, arguments: list[str], pattern: str) -> str:
    path = shutil.which(executable)
    if not path:
        raise PreflightError(f"Missing required executable on PATH: {executable}")
    try:
        result = subprocess.run(
            [path, *arguments], check=True, capture_output=True, text=True,
            timeout=12, cwd=ROOT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise PreflightError(f"Could not verify {executable}: {type(error).__name__}") from error
    match = re.search(pattern, result.stdout + "\n" + result.stderr, re.MULTILINE)
    if not match:
        raise PreflightError(f"Unexpected {executable} --version output")
    return match.group(1)


def preflight() -> str:
    java = checked_version("java", ["-version"], r'(?:openjdk|java)(?: version)?\s+"?(\d+)')
    javac = checked_version("javac", ["-version"], r"javac (\d+)")
    if int(java) < 17 or int(javac) < 17:
        raise PreflightError("JDK 17 or newer is required")
    gradle = checked_version("gradle", ["--version"], r"^Gradle (\d+\.\d+(?:\.\d+)?)$")
    gradle_parts = gradle.split(".")
    if tuple(map(int, gradle_parts[:2])) != (8, 13):
        raise PreflightError("Expected an already installed Gradle 8.13.x; no wrapper download is permitted")

    android_home = os.environ.get("ANDROID_HOME")
    android_sdk_root = os.environ.get("ANDROID_SDK_ROOT")
    if android_home and android_sdk_root and Path(android_home).resolve() != Path(android_sdk_root).resolve():
        raise PreflightError("ANDROID_HOME and ANDROID_SDK_ROOT disagree")
    sdk = Path(android_home or android_sdk_root or "")
    if not (android_home or android_sdk_root) or not sdk.is_dir():
        raise PreflightError("ANDROID_HOME or ANDROID_SDK_ROOT must point to an existing SDK")
    if not (sdk / "platforms" / "android-36" / "android.jar").is_file():
        raise PreflightError("SDK Platform 36 is not installed; offline build cannot download it")
    if not (sdk / "build-tools" / "35.0.0").is_dir():
        raise PreflightError("Android Build Tools 35.0.0 are not installed")
    return shutil.which("gradle") or "gradle"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build REALM Android debug APK without dependency downloads")
    parser.add_argument("--preflight", action="store_true", help="Check tools and SDK only; do not build")
    args = parser.parse_args()
    try:
        gradle = preflight()
        print("Preflight PASS: JDK, Gradle 8.13.x, Android SDK 36 and Build Tools 35.0.0 present")
        if args.preflight:
            return 0
        env = dict(os.environ)
        # A configured proxy is never required for this offline dependency build.
        env.pop("GRADLE_OPTS", None)
        env["GRADLE_OPTS"] = "-Dorg.gradle.java.installations.auto-download=false"
        command = [
            gradle, "--offline", "--no-daemon", "--no-watch-fs", "--console=plain",
            ":app:testDebugUnitTest", ":app:assembleDebug",
        ]
        subprocess.run(command, cwd=ROOT, env=env, check=True)
        if not APK.is_file() or APK.stat().st_size == 0:
            raise PreflightError("Gradle returned success without a nonempty debug APK")
        with APK.open("rb") as artifact:
            digest = hashlib.file_digest(artifact, "sha256").hexdigest()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            capture_output=True, check=False, timeout=10,
        )
        print(f"APK_PATH={APK}")
        print(f"APK_SHA256={digest}")
        print(f"SOURCE_HEAD={commit.stdout.strip() if commit.returncode == 0 else 'UNKNOWN'}")
        print("QUALIFICATION=local-debug-build-and-unit-tests-only; no phone test or HTTPS route test")
        return 0
    except PreflightError as error:
        print(f"OFFLINE_BUILD_BLOCKED: {error}", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"OFFLINE_BUILD_FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
