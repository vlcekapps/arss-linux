project(
  'arss-linux',
  version: '1.7.0',
  meson_version: '>=1.2.0',
)

python = import('python').find_installation('python3')

# Fedora's GTK stack is distributed as runtime packages with introspection
# data, not as Python wheels or development pkg-config files.  Validate the
# exact interpreter and GI runtime that the installed launcher will use.
runtime_check = run_command(
  python,
  '-c',
  '''
import sys


def release(value):
    numbers = []
    for component in value.split(".")[:2]:
        digits = ""
        for character in component:
            if not character.isdigit():
                break
            digits += character
        numbers.append(int(digits or 0))
    return tuple(numbers)


try:
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required")

    import requests
    if release(requests.__version__) < (2, 31):
        raise RuntimeError("Requests 2.31 or newer is required")

    import gi
    if gi.version_info < (3, 48, 0):
        raise RuntimeError("PyGObject 3.48 or newer is required")
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    gi.require_version("Gst", "1.0")
    from gi.repository import Adw, Gst, Gtk

    if (Gtk.get_major_version(), Gtk.get_minor_version()) < (4, 20):
        raise RuntimeError("GTK 4.20 or newer is required")
    if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 7):
        raise RuntimeError("libadwaita 1.7 or newer is required")

    Gst.init(None)
    if not (Gst.ElementFactory.find("playbin3") or Gst.ElementFactory.find("playbin")):
        raise RuntimeError("a GStreamer playbin plugin is required")
except Exception as error:
    print(error, file=sys.stderr)
    raise SystemExit(1)
''',
  check: false,
)
if runtime_check.returncode() != 0
  error('ARSS runtime dependency check failed: ' + runtime_check.stderr().strip())
endif

python.install_sources(
  [
    'arss/__init__.py',
    'arss/__main__.py',
    'arss/application.py',
    'arss/audio_session.py',
    'arss/background.py',
    'arss/checkpoints.py',
    'arss/contract.py',
    'arss/directory.py',
    'arss/feed.py',
    'arss/gtk_helpers.py',
    'arss/guide.py',
    'arss/guide_catalog.py',
    'arss/i18n.py',
    'arss/models.py',
    'arss/monitor.py',
    'arss/mpris.py',
    'arss/notifications.py',
    'arss/opml.py',
    'arss/playback.py',
    'arss/search.py',
    'arss/storage.py',
    'arss/ui.py',
  ],
  subdir: 'arss',
)

install_subdir(
  'arss/data',
  install_dir: join_paths(python.get_install_dir(), 'arss'),
  exclude_directories: ['sounds'],
)

launcher_configuration = configuration_data()
launcher_configuration.set('PYTHON', python.full_path())
configured_bindir = get_option('bindir')
if not configured_bindir.startswith('/')
  configured_bindir = join_paths(get_option('prefix'), configured_bindir)
endif
launcher_configuration.set(
  'ARSS_LAUNCHER',
  join_paths(configured_bindir, 'arss'),
)
configure_file(
  input: 'data/arss.in',
  output: 'arss',
  configuration: launcher_configuration,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

configure_file(
  input: 'data/cz.pvlcek.arss.desktop.in',
  output: 'cz.pvlcek.arss.desktop',
  configuration: launcher_configuration,
  install: true,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)
configure_file(
  input: 'data/cz.pvlcek.arss.service.in',
  output: 'cz.pvlcek.arss.service',
  configuration: launcher_configuration,
  install: true,
  install_dir: join_paths(get_option('datadir'), 'dbus-1', 'services'),
)
systemd_user_unit_dir = get_option('systemd_user_unit_dir')
if systemd_user_unit_dir == ''
  systemd_user_unit_dir = join_paths(get_option('datadir'), 'systemd', 'user')
endif
configure_file(
  input: 'data/arss-monitor@.service.in',
  output: 'arss-monitor@.service',
  configuration: launcher_configuration,
  install: true,
  install_dir: systemd_user_unit_dir,
)
install_data(
  'data/arss-monitor@.timer',
  install_dir: systemd_user_unit_dir,
)
install_data(
  'data/cz.pvlcek.arss.metainfo.xml',
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)
install_data(
  'data/arss.xml',
  install_dir: join_paths(get_option('datadir'), 'swcatalog', 'xml'),
)
install_data(
  'data/cz.pvlcek.arss.svg',
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)

install_data(
  'LICENSE',
  install_dir: join_paths(get_option('datadir'), 'licenses', 'arss-linux'),
)
install_data(
  'docs/android-parity.md',
  'docs/contract.md',
  'docs/desktop-integration.md',
  install_dir: join_paths(get_option('datadir'), 'doc', 'arss-linux'),
)
