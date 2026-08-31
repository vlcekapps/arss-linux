#!/usr/bin/bash
set -euo pipefail

# Build only from local files so release artifacts remain reproducible and do
# not depend on network availability.
script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_directory="$(cd -- "${script_directory}/.." && pwd)"
spec_file="${project_directory}/packaging/arss.spec"
rpm_release="${ARSS_RPM_RELEASE:-1}"
source_date_epoch="${SOURCE_DATE_EPOCH:-1788134400}"
output_directory="${ARSS_RPM_OUTPUT:-${project_directory}/dist/rpm}"

if [[ ! "${rpm_release}" =~ ^[1-9][0-9]*$ ]]; then
    echo "ARSS_RPM_RELEASE must be a positive integer." >&2
    exit 2
fi
if [[ ! "${source_date_epoch}" =~ ^[0-9]+$ ]]; then
    echo "SOURCE_DATE_EPOCH must be a non-negative integer." >&2
    exit 2
fi

meson_version="$(sed -n "s/^[[:space:]]*version: '\([^']*\)'.*/\1/p" "${project_directory}/meson.build")"
python_version="$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "${project_directory}/arss/__init__.py")"
project_version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' "${project_directory}/pyproject.toml" | head -n 1)"
appstream_version="$(sed -n 's/.*<release version="\([^"]*\)".*/\1/p' "${project_directory}/data/cz.pvlcek.arss.metainfo.xml" | head -n 1)"
spec_version="$(sed -n 's/^Version:[[:space:]]*\([^[:space:]]*\).*/\1/p' "${spec_file}")"

if [[ -z "${meson_version}" ]] || [[ "${meson_version}" != "${python_version}" ]] || \
   [[ "${meson_version}" != "${project_version}" ]] || \
   [[ "${meson_version}" != "${appstream_version}" ]] || \
   [[ "${meson_version}" != "${spec_version}" ]]; then
    echo "Version mismatch; update Meson, Python, pyproject, AppStream, and RPM spec together." >&2
    printf 'Meson=%s Python=%s pyproject=%s AppStream=%s spec=%s\n' \
        "${meson_version}" "${python_version}" "${project_version}" \
        "${appstream_version}" "${spec_version}" >&2
    exit 2
fi

python3 "${project_directory}/tools/validate_appstream_catalog.py"

work_directory="$(mktemp -d -p /tmp arss-rpm-build.XXXXXXXX)"
trap 'rm -rf -- "${work_directory}"' EXIT
source_copy="${work_directory}/source"
sdist_directory="${work_directory}/sdist"
repack_directory="${work_directory}/repack"
rpm_top_directory="${work_directory}/rpmbuild"
rpm_temp_directory="${work_directory}/tmp"

mkdir -p -- "${source_copy}" "${sdist_directory}" "${repack_directory}" \
    "${rpm_temp_directory}"
mkdir -p -- "${rpm_top_directory}/BUILD" "${rpm_top_directory}/BUILDROOT"
mkdir -p -- "${rpm_top_directory}/RPMS" "${rpm_top_directory}/SOURCES"
mkdir -p -- "${rpm_top_directory}/SPECS" "${rpm_top_directory}/SRPMS"

# Work on a disposable copy because setuptools generates egg-info while it
# evaluates MANIFEST.in.
cp -a -- "${project_directory}/." "${source_copy}/"
rm -rf -- "${source_copy}/.agents" "${source_copy}/.codex" \
    "${source_copy}/.git" "${source_copy}/_build" "${source_copy}/build" \
    "${source_copy}/dist" "${source_copy}/arss_linux.egg-info"
find "${source_copy}" -type d -name __pycache__ -prune -exec rm -rf -- {} +
find "${source_copy}" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

(
    cd -- "${source_copy}"
    SOURCE_DATE_EPOCH="${source_date_epoch}" python3 -c \
        'import sys; from setuptools.build_meta import build_sdist; build_sdist(sys.argv[1])' \
        "${sdist_directory}"
)

generated_sdist="${sdist_directory}/arss_linux-${meson_version}.tar.gz"
if [[ ! -f "${generated_sdist}" ]]; then
    echo "setuptools did not create the expected source archive: ${generated_sdist}" >&2
    exit 1
fi

tar -xzf "${generated_sdist}" -C "${repack_directory}"
mv -- "${repack_directory}/arss_linux-${meson_version}" \
    "${repack_directory}/arss-${meson_version}"
find "${repack_directory}/arss-${meson_version}" -exec \
    touch -h -d "@${source_date_epoch}" -- {} +

source_tar="${rpm_top_directory}/SOURCES/arss-${meson_version}.tar"
tar --sort=name --format=gnu --owner=0 --group=0 --numeric-owner \
    --mtime="@${source_date_epoch}" -C "${repack_directory}" \
    -cf "${source_tar}" "arss-${meson_version}"
gzip -n -- "${source_tar}"
install -m 0644 -- "${spec_file}" "${rpm_top_directory}/SPECS/arss.spec"

SOURCE_DATE_EPOCH="${source_date_epoch}" rpmbuild -ba \
    --define "_topdir ${rpm_top_directory}" \
    --define "_tmppath ${rpm_temp_directory}" \
    --define "_buildhost localhost" \
    --define "_smp_build_ncpus 1" \
    --define "arss_release ${rpm_release}" \
    --define "use_source_date_epoch_as_buildtime 1" \
    "${rpm_top_directory}/SPECS/arss.spec"

mkdir -p -- "${output_directory}"
install -m 0644 -- "${source_tar}.gz" "${output_directory}/"
mapfile -d '' artifacts < <(
    find "${rpm_top_directory}/RPMS" "${rpm_top_directory}/SRPMS" \
        -type f -name '*.rpm' -print0 | sort -z
)
if (( ${#artifacts[@]} == 0 )); then
    echo "rpmbuild completed without producing an RPM." >&2
    exit 1
fi

for artifact in "${artifacts[@]}"; do
    install -m 0644 -- "${artifact}" "${output_directory}/"
done

echo "Normalized source archive:"
sha256sum -- "${output_directory}/$(basename -- "${source_tar}.gz")"
echo "RPM artifacts:"
for artifact in "${artifacts[@]}"; do
    installed_artifact="${output_directory}/$(basename -- "${artifact}")"
    sha256sum -- "${installed_artifact}"
done
