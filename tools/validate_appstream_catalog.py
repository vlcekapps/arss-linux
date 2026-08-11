#!/usr/bin/python3
"""Validate ARSS metainfo and the local-RPM AppStream catalog.

The catalog deliberately duplicates a small upstream metadata component because
there is no repository-generated catalog for a locally installed RPM.  Keep the
copy exact and make sure AppStream recognizes its package association.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree


COMPONENT_ID = "cz.pvlcek.arss"
PACKAGE_NAME = "arss"
XML_LANGUAGE = "{http://www.w3.org/XML/1998/namespace}lang"
SHARED_TAGS = (
    "id",
    "metadata_license",
    "project_license",
    "name",
    "summary",
    "launchable",
    "provides",
    "url",
    "developer",
    "content_rating",
    "releases",
)


class MetadataError(ValueError):
    """Raised when the two metadata sources cannot safely be packaged."""


def _normalized_text(value: str | None) -> str:
    return " ".join((value or "").split())


def _canonical_element(element: ElementTree.Element) -> tuple[object, ...]:
    """Return a whitespace-insensitive representation of one XML subtree."""

    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        _normalized_text(element.text),
        tuple(_canonical_element(child) for child in element),
    )


def _tag_values(component: ElementTree.Element, tag: str) -> list[tuple[object, ...]]:
    return sorted(
        (_canonical_element(element) for element in component.findall(tag)),
        key=repr,
    )


def _canonical_description_child(
    element: ElementTree.Element,
) -> tuple[object, ...]:
    """Canonicalize content after removing format-specific locale placement."""

    return (
        element.tag,
        tuple(
            sorted(
                (name, value)
                for name, value in element.attrib.items()
                if name != XML_LANGUAGE
            )
        ),
        _normalized_text(element.text),
        tuple(_canonical_description_child(child) for child in element),
    )


def _localized_descriptions(
    component: ElementTree.Element,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    localized: dict[str, list[tuple[object, ...]]] = {}
    for description in component.findall("description"):
        section_language = description.get(XML_LANGUAGE, "C")
        for child in description:
            language = child.get(XML_LANGUAGE, section_language)
            localized.setdefault(language, []).append(
                _canonical_description_child(child)
            )
    return {
        language: tuple(values)
        for language, values in sorted(localized.items())
    }


def _load_metainfo(path: Path) -> ElementTree.Element:
    root = ElementTree.parse(path).getroot()
    if root.tag != "component":
        raise MetadataError(f"{path}: expected a metainfo <component> root")
    return root


def _load_catalog(path: Path) -> tuple[ElementTree.Element, ElementTree.Element]:
    root = ElementTree.parse(path).getroot()
    if root.tag != "components":
        raise MetadataError(f"{path}: expected a catalog <components> root")
    if root.get("origin") != "arss-local":
        raise MetadataError(f"{path}: catalog origin must be 'arss-local'")
    components = root.findall("component")
    if len(components) != 1:
        raise MetadataError(f"{path}: expected exactly one catalog component")
    return root, components[0]


def validate_sources(metainfo_path: Path, catalog_path: Path) -> None:
    metainfo = _load_metainfo(metainfo_path)
    _catalog_root, catalog = _load_catalog(catalog_path)

    for label, component in (("metainfo", metainfo), ("catalog", catalog)):
        if component.get("type") != "desktop-application":
            raise MetadataError(f"{label}: component type must be 'desktop-application'")
        if _normalized_text(component.findtext("id")) != COMPONENT_ID:
            raise MetadataError(f"{label}: component id must be {COMPONENT_ID!r}")

    if catalog.get("merge") is not None:
        raise MetadataError(
            "catalog: the component must be standalone, not an orphan merge directive"
        )

    package_names = [
        _normalized_text(element.text) for element in catalog.findall("pkgname")
    ]
    if package_names != [PACKAGE_NAME]:
        raise MetadataError(
            f"catalog: expected exactly <pkgname>{PACKAGE_NAME}</pkgname>"
        )
    if metainfo.findall("pkgname"):
        raise MetadataError("metainfo: pkgname is distribution data and must stay in the catalog")

    for tag in SHARED_TAGS:
        upstream_values = _tag_values(metainfo, tag)
        catalog_values = _tag_values(catalog, tag)
        if not upstream_values:
            raise MetadataError(f"metainfo: required <{tag}> data is missing")
        if upstream_values != catalog_values:
            raise MetadataError(
                f"catalog: <{tag}> data differs from {metainfo_path.name}"
            )

    metainfo_descriptions = _localized_descriptions(metainfo)
    catalog_descriptions = _localized_descriptions(catalog)
    if not metainfo_descriptions:
        raise MetadataError("metainfo: required <description> data is missing")
    if metainfo_descriptions != catalog_descriptions:
        raise MetadataError(
            f"catalog: localized <description> data differs from {metainfo_path.name}"
        )

    stock_icons = [
        _normalized_text(icon.text)
        for icon in catalog.findall("icon")
        if icon.get("type") == "stock"
    ]
    if stock_icons != [COMPONENT_ID]:
        raise MetadataError(
            f"catalog: expected one stock icon named {COMPONENT_ID!r}"
        )

    categories = {
        _normalized_text(category.text)
        for category in catalog.findall("categories/category")
    }
    required_categories = {"Network", "News"}
    if not required_categories.issubset(categories):
        missing = ", ".join(sorted(required_categories - categories))
        raise MetadataError(f"catalog: required categories missing: {missing}")

    czech_summaries = [
        summary
        for summary in catalog.findall("summary")
        if summary.get(XML_LANGUAGE) == "cs" and _normalized_text(summary.text)
    ]
    descriptions = _localized_descriptions(catalog)
    if len(czech_summaries) != 1 or "cs" not in descriptions:
        raise MetadataError("catalog: Czech summary and description must be present once")


def _run_appstreamcli(metainfo_path: Path, catalog_path: Path) -> None:
    executable = shutil.which("appstreamcli")
    if executable is None:
        raise MetadataError("appstreamcli is required to validate distribution metadata")

    for path in (metainfo_path, catalog_path):
        result = subprocess.run(
            [executable, "validate", "--no-net", "--strict", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise MetadataError(
                f"appstreamcli rejected {path}:\n{result.stdout.rstrip()}"
            )

    # Conversion parses only this catalog file, so the check cannot be masked by
    # a similarly named component in the host's global AppStream cache.
    with tempfile.TemporaryDirectory(prefix="arss-appstream-") as temporary:
        yaml_path = Path(temporary) / "arss.yml"
        result = subprocess.run(
            [executable, "convert", str(catalog_path), str(yaml_path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise MetadataError(
                f"appstreamcli could not parse {catalog_path}:\n{result.stdout.rstrip()}"
            )
        converted = yaml_path.read_text(encoding="utf-8")

    required_lines = {
        "component id": rf"(?m)^ID:\s*{re.escape(COMPONENT_ID)}\s*$",
        "package name": rf"(?m)^Package:\s*{re.escape(PACKAGE_NAME)}\s*$",
        "component type": r"(?m)^Type:\s*desktop-application\s*$",
    }
    for label, pattern in required_lines.items():
        if re.search(pattern, converted) is None:
            raise MetadataError(
                f"appstreamcli conversion lost the catalog {label}; output was:\n{converted}"
            )


def _run_isolated_appstream_pool(
    metainfo_path: Path,
    catalog_path: Path,
) -> None:
    """Confirm that catalog and metainfo merge into one package-backed app."""

    with tempfile.TemporaryDirectory(prefix="arss-appstream-pool-") as temporary:
        root = Path(temporary)
        catalog_directory = root / "catalog"
        metainfo_directory = root / "metainfo"
        catalog_directory.mkdir()
        metainfo_directory.mkdir()
        shutil.copy2(catalog_path, catalog_directory / catalog_path.name)
        shutil.copy2(metainfo_path, metainfo_directory / metainfo_path.name)

        previous_cache_home = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        try:
            import gi

            gi.require_version("AppStream", "1.0")
            from gi.repository import AppStream

            pool = AppStream.Pool.new()
            pool.set_load_std_data_locations(False)
            pool.add_extra_data_location(
                str(catalog_directory), AppStream.FormatStyle.CATALOG
            )
            pool.add_extra_data_location(
                str(metainfo_directory), AppStream.FormatStyle.METAINFO
            )
            loaded = pool.load()
        except (ImportError, ValueError) as error:
            raise MetadataError(
                "AppStream GI bindings are required for the isolated pool check"
            ) from error
        except Exception as error:
            raise MetadataError(f"isolated AppStream pool failed: {error}") from error
        finally:
            if previous_cache_home is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = previous_cache_home

        if not loaded:
            raise MetadataError("isolated AppStream pool did not load")

        components = pool.get_components_by_id(COMPONENT_ID).as_array()
        if len(components) != 1:
            raise MetadataError(
                "isolated AppStream pool did not merge catalog and metainfo "
                f"into one {COMPONENT_ID} component"
            )
        component = components[0]
        if component.get_pkgname() != PACKAGE_NAME:
            raise MetadataError(
                "isolated AppStream pool lost the component-to-package association"
            )

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    project_root = Path(__file__).resolve().parent.parent
    parser.add_argument(
        "--metainfo",
        type=Path,
        default=project_root / "data" / "cz.pvlcek.arss.metainfo.xml",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=project_root / "data" / "arss.xml",
    )
    arguments = parser.parse_args(argv)

    try:
        validate_sources(arguments.metainfo, arguments.catalog)
        _run_appstreamcli(arguments.metainfo, arguments.catalog)
        _run_isolated_appstream_pool(arguments.metainfo, arguments.catalog)
    except (MetadataError, ElementTree.ParseError, OSError) as error:
        print(f"AppStream metadata validation failed: {error}", file=sys.stderr)
        return 1

    print(
        f"AppStream catalog maps {COMPONENT_ID} to RPM package {PACKAGE_NAME} "
        "and matches metainfo."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
