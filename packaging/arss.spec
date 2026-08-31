%{!?arss_release:%global arss_release 1}

Name:           arss
Version:        1.7.0
Release:        %{arss_release}%{?dist}
Summary:        Accessible GTK 4 feed, podcast, and programme reader

License:        GPL-3.0-or-later AND CC0-1.0 AND MIT
URL:            https://github.com/vlcekapps/arss-linux
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  appstream
BuildRequires:  desktop-file-utils
BuildRequires:  gstreamer1
BuildRequires:  gstreamer1-plugins-base
BuildRequires:  gtk4 >= 4.20
BuildRequires:  libadwaita >= 1.7
BuildRequires:  meson >= 1.2.0
BuildRequires:  python3-devel >= 3.11
BuildRequires:  python3-gobject >= 3.48
BuildRequires:  python3-requests >= 2.31
BuildRequires:  systemd-rpm-macros

Requires:       gstreamer1
Requires:       gstreamer1-plugins-bad-free
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       gstreamer1-plugins-ugly-free
Requires:       gtk4 >= 4.20
Requires:       hicolor-icon-theme
Requires:       libadwaita >= 1.7
Requires:       python3 >= 3.11
Requires:       python3-gobject >= 3.48
Requires:       python3-requests >= 2.31
Requires:       systemd
Recommends:     wireplumber-libs >= 0.5.11

%description
ARSS is a native, accessible GTK 4 application for RSS and Atom feeds,
podcasts, and Czech television and radio schedules.

%prep
%autosetup -n %{name}-%{version}

%build
%meson -Dsystemd_user_unit_dir=%{_userunitdir}
%meson_build

%install
%meson_install

%check
%{__python3} -m unittest discover -s tests -v
desktop-file-validate %{_vpath_builddir}/cz.pvlcek.arss.desktop
%{__python3} tools/validate_appstream_catalog.py
systemd-analyze verify redhat-linux-build/arss-monitor@.service data/arss-monitor@.timer

%files
%{_bindir}/arss
%{python3_sitelib}/arss/
%{_datadir}/applications/cz.pvlcek.arss.desktop
%{_datadir}/dbus-1/services/cz.pvlcek.arss.service
%{_datadir}/icons/hicolor/scalable/apps/cz.pvlcek.arss.svg
%{_datadir}/metainfo/cz.pvlcek.arss.metainfo.xml
%{_datadir}/swcatalog/xml/arss.xml
%{_userunitdir}/arss-monitor@.service
%{_userunitdir}/arss-monitor@.timer
%license %{_datadir}/licenses/arss-linux/LICENSE
%license %{python3_sitelib}/arss/data/contract/LICENSE
%license %{python3_sitelib}/arss/data/contract/THIRD_PARTY_NOTICES.md
%doc %{_datadir}/doc/arss-linux/android-parity.md
%doc %{_datadir}/doc/arss-linux/contract.md
%doc %{_datadir}/doc/arss-linux/desktop-integration.md

%changelog
* Mon Aug 31 2026 Pavel Vlček <pavel@example.invalid> - 1.7.0-1
- Consume the verified cross-platform ARSS Contract without a Git submodule
- Use stable station IDs and migrate legacy provider-specific preferences
- Expand the offline television and radio catalogue with golden parity tests

* Tue Aug 11 2026 Pavel Vlček <pavel@example.invalid> - 1.6.12-2
- Keep LICENSE canonical so repository hosts detect GNU GPL version 3
- Retain the GPL-3.0-or-later choice in project and package metadata

* Tue Aug 11 2026 Pavel Vlček <pavel@example.invalid> - 1.6.12-1
- Remove bundled notification audio and delegate sound policy to GNOME
- Publish the clean Linux source under GPL-3.0-or-later
- Migrate obsolete custom-sound preferences without breaking upgrades

* Mon Aug 03 2026 Pavel Vlček <pavel@example.invalid> - 1.6.11-1
- Keep the GNOME application entry valid while RPM replaces /usr/bin/arss
- Add a regression contract for the stable desktop Exec program

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.10-1
- Complete the Linux a11y guide audit for menus and 200 percent text reflow
- Keep full main-tab labels visible and focused after activation
- Add English, Czech and High Contrast large-text release gates

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.9-1
- Add Nova Sport 3 through 6 from the verified SMS.cz programme source
- Keep clean display names separate from stable provider identifiers

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.8-1
- Group television stations by broadcaster with accessible native search
- Preserve station IDs across sorting, saved selection and programme loading
- Move add, directory and OPML actions into named RSS and podcast menus

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.7-1
- Remove the ambiguously announced background-check switch mnemonic
- Preserve its native GTK role, accessible description and keyboard control

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.6-1
- Add an accessible 0-100 percent podcast volume control and MPRIS volume
- Keep podcast playback running across window and application focus changes
- Make programme details and empty feed lists keyboard-readable

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.5-1
- Restore native arrow-key navigation in feeds and article lists
- Expose named activatable list items without duplicate primary focus stops
- Apply the same keyboard contract to podcasts, search results and programmes

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 1.6.4-1
- Complete Android 1.6.4 feature parity in the native GTK 4 interface
- Add accessible navigation, focused controls and compact OPML menus
- Add opt-in systemd monitoring, GNOME notifications, MPRIS and audio roles
- Add package-backed AppStream catalog metadata for GNOME Software

* Sun Aug 02 2026 Pavel Vlček <pavel@example.invalid> - 0.1.0-1
- Initial Fedora package for the GTK 4 port
