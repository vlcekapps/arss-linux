from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from xml.etree import ElementTree

from arss import __version__


ROOT = Path(__file__).resolve().parent.parent


def render_desktop_entry(*, python: str, launcher: str) -> str:
    template = (
        ROOT / "data" / "cz.pvlcek.arss.desktop.in"
    ).read_text(encoding="utf-8")
    return template.replace("@PYTHON@", python).replace(
        "@ARSS_LAUNCHER@", launcher
    )


def rpm_desktop_entry() -> str:
    return render_desktop_entry(
        python="/usr/bin/python3",
        launcher="/usr/bin/arss",
    )


def render_launcher_template(relative_path: str) -> str:
    return (
        ROOT / relative_path
    ).read_text(encoding="utf-8").replace(
        "@PYTHON@", "/usr/bin/python3"
    ).replace(
        "@ARSS_LAUNCHER@", "/usr/bin/arss"
    )


class PackagingContractTest(unittest.TestCase):
    def test_ci_targets_fedora_45_with_the_headless_gles_runtime(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("container: fedora:45", workflow)
        self.assertIn("libglvnd-gles", workflow)

    def test_ci_handoff_contains_one_binary_rpm_and_its_sha_only(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "offline-tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("-name 'arss-*.noarch.rpm' ! -name '*.src.rpm'", workflow)
        self.assertIn('sha256sum -- "${artifact_name}"', workflow)
        upload = workflow.split("- name: Upload the binary Fedora RPM", 1)[1]
        self.assertIn("ci-artifacts/fedora-rpm/*.noarch.rpm", upload)
        self.assertIn("ci-artifacts/fedora-rpm/*.noarch.rpm.sha256", upload)
        self.assertNotIn("dist/rpm", upload)
        self.assertNotIn("*.src.rpm", upload)

    def test_vendored_contract_lock_and_licenses_are_packaged(self) -> None:
        contract = ROOT / "arss" / "data" / "contract"
        lock = json.loads(
            (contract / "contract.lock.json").read_text(encoding="utf-8")
        )
        manifest = (contract / "manifest.sha256").read_bytes()
        self.assertEqual(
            "https://github.com/vlcekapps/arss-contract",
            lock["sourceRepository"],
        )
        self.assertEqual(
            f"v{lock['contractVersion']}",
            lock["sourceTag"],
        )
        self.assertRegex(lock["sourceCommit"], r"^[0-9a-f]{40}$")
        self.assertEqual(
            hashlib.sha256(manifest).hexdigest(),
            lock["manifestSha256"],
        )

        manifest_entries = {
            relative: digest
            for digest, relative in (
                line.split("  ", 1)
                for line in manifest.decode("ascii").splitlines()
            )
        }
        for relative in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            with self.subTest(relative=relative):
                payload = (contract / relative).read_bytes()
                self.assertEqual(
                    hashlib.sha256(payload).hexdigest(),
                    manifest_entries[relative],
                )

        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "arss.spec").read_text(encoding="utf-8")
        self.assertIn('"arss/data/contract/LICENSE"', pyproject)
        self.assertIn(
            '"arss/data/contract/THIRD_PARTY_NOTICES.md"',
            pyproject,
        )
        self.assertIn("data/contract/LICENSE", spec)
        self.assertIn("data/contract/THIRD_PARTY_NOTICES.md", spec)

    def test_release_versions_match(self) -> None:
        meson = (ROOT / "meson.build").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        spec = (ROOT / "packaging" / "arss.spec").read_text(encoding="utf-8")
        metainfo = ElementTree.parse(
            ROOT / "data" / "cz.pvlcek.arss.metainfo.xml"
        ).getroot()
        catalog = ElementTree.parse(ROOT / "data" / "arss.xml").getroot()
        meson_version = re.search(r"version:\s*'([^']+)'", meson)
        project_version = re.search(r'(?m)^version = "([^"]+)"$', pyproject)
        spec_version = re.search(r"(?m)^Version:\s*(\S+)$", spec)
        release = metainfo.find("releases/release")
        catalog_release = catalog.find("component/releases/release")
        self.assertIsNotNone(meson_version)
        self.assertIsNotNone(project_version)
        self.assertIsNotNone(spec_version)
        self.assertIsNotNone(release)
        self.assertIsNotNone(catalog_release)
        self.assertEqual(
            {
                __version__,
                meson_version.group(1),
                project_version.group(1),
                spec_version.group(1),
                release.get("version"),
                catalog_release.get("version"),
            },
            {__version__},
        )

    def test_desktop_identity_and_activation_contract_are_stable(self) -> None:
        desktop = rpm_desktop_entry()
        values = dict(
            line.split("=", 1)
            for line in desktop.splitlines()
            if "=" in line
        )
        self.assertEqual(
            ["/usr/bin/python3", "/usr/bin/arss"],
            shlex.split(values["Exec"]),
        )
        self.assertEqual("true", values["DBusActivatable"])
        self.assertEqual("true", values["StartupNotify"])
        self.assertEqual("cz.pvlcek.arss", values["Icon"])
        self.assertNotIn("TryExec", values)

    def test_all_packaged_launchers_use_the_stable_interpreter(self) -> None:
        dbus_service = render_launcher_template(
            "data/cz.pvlcek.arss.service.in"
        )
        monitor_service = render_launcher_template(
            "data/arss-monitor@.service.in"
        )
        dbus_exec = next(
            line.split("=", 1)[1]
            for line in dbus_service.splitlines()
            if line.startswith("Exec=")
        )
        monitor_exec = next(
            line.split("=", 1)[1]
            for line in monitor_service.splitlines()
            if line.startswith("ExecStart=")
        )
        self.assertEqual(
            ["/usr/bin/python3", "/usr/bin/arss", "--gapplication-service"],
            shlex.split(dbus_exec),
        )
        self.assertEqual(
            [
                "/usr/bin/python3",
                "/usr/bin/arss",
                "--background-check",
                "%i",
            ],
            shlex.split(monitor_exec),
        )

    def test_gio_keeps_dbus_activatable_entry_while_launcher_is_missing(self) -> None:
        probe = """
import gi
gi.require_version("GioUnix", "2.0")
from gi.repository import GioUnix

try:
    entry = GioUnix.DesktopAppInfo.new("cz.pvlcek.arss.desktop")
except TypeError:
    entry = None
raise SystemExit(0 if entry is not None else 3)
"""
        with tempfile.TemporaryDirectory(prefix="arss-desktop-test-") as temporary:
            root = Path(temporary)
            applications = root / "applications"
            applications.mkdir()
            launcher = root / "temporarily-missing-arss"
            desktop_path = applications / "cz.pvlcek.arss.desktop"
            desktop_path.write_text(
                render_desktop_entry(
                    python="/usr/bin/python3",
                    launcher=str(launcher),
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["XDG_DATA_HOME"] = str(root)
            environment["XDG_DATA_DIRS"] = str(root / "no-system-data")

            valid = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                env=environment,
            )
            self.assertEqual(0, valid.returncode)

            desktop_path.write_text(
                render_desktop_entry(
                    python=str(root / "temporarily-missing-python"),
                    launcher=str(launcher),
                ),
                encoding="utf-8",
            )
            invalid = subprocess.run(
                [sys.executable, "-c", probe],
                check=False,
                env=environment,
            )
            self.assertEqual(3, invalid.returncode)


if __name__ == "__main__":
    unittest.main()
