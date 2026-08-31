# ARSS pro Linux

ARSS je nativní a přístupná aplikace GTK 4 pro RSS, Atom, podcasty a český
televizní a rozhlasový program. Linuxový port používá Python 3, PyGObject,
libadwaitu a GStreamer. Články vždy otevírá ve výchozím webovém prohlížeči;
podcastové epizody přehrává uvnitř aplikace.

## Současný stav

Port zachovává kompatibilní soubory `readFeeds.opml` a `podcasts.opml`, bezpečné
limity síťových odpovědí a OPML, české i anglické rozhraní, lokální RSS katalog,
podcastové katalogy, TV/rozhlasový program, interní přehrávač a oznámení nových
položek. Uživatelská data jsou v
`$XDG_DATA_HOME/arss`, nastavení v `$XDG_CONFIG_HOME/arss` a omezené checkpointy
automatických kontrol v `$XDG_STATE_HOME/arss`.

Zvukovou signalizaci oznámení vybírá GNOME podle systémového nastavení. ARSS
nepřibaluje ani samostatně nepřehrává vlastní zvuky oznámení.

Bez výslovného zapnutí běží automatické kontroly pouze po dobu otevřené
aplikace. Volitelné kontroly po jejím ukončení používají uživatelské systemd
timery popsané níže; instalace balíčku je sama nikdy nezapne.

## Spuštění na Fedoře

Na Fedoře nainstalujte runtime prostředí z distribuce. GTK, libadwaita,
PyGObject a GStreamer jsou systémové komponenty; neinstalujte je do projektu
pomocí `pip`.

```bash
sudo dnf install \
  python3 python3-gobject python3-requests gtk4 libadwaita \
  gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
  gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free
```

Aplikaci lze spustit přímo ze zdrojového stromu:

```bash
git clone https://github.com/vlcekapps/arss-linux.git
cd arss-linux
./run.sh
```

Vyžaduje Python 3.11+, PyGObject 3.48+, GTK 4.20+, libadwaita 1.7+,
Requests 2.31+ a GStreamer s prvkem `playbin` nebo `playbin3`.

Vyhledávání kombinuje veřejné bezklíčové hledání Podcast Indexu a regionální
Apple Podcasts stejně jako Android aplikace. Pokud prostředí obsahuje oba údaje
`PODCAST_INDEX_KEY` a `PODCAST_INDEX_SECRET`, ARSS místo veřejného kompatibilního
endpointu použije současné podepsané API Podcast Indexu. Tyto přístupové údaje
neukládejte do repozitáře.

## Instalace desktopové aplikace

Úplná instalace včetně spouštěče, ikony, položky v nabídce aplikací a AppStream
metadat používá Meson. Pro instalaci jen pro aktuálního uživatele není potřeba
oprávnění správce:

```bash
sudo dnf install meson
cd arss-linux
meson setup _build --prefix="$HOME/.local"
meson install -C _build
```

Spouštěč se nainstaluje jako `$HOME/.local/bin/arss`. Pokud tento adresář není
v `PATH`, lze aplikaci poprvé spustit úplnou cestou. Příkaz `pip install .`
instaluje pouze modul a konzolový spouštěč; desktopová metadata proto nenahrazuje
instalaci přes Meson. Meson při konfiguraci ověří verze Pythonu, GTK,
libadwaity, PyGObjectu a Requests i dostupnost přehrávače GStreamer.

## Volitelné kontroly na pozadí

První podporované prostředí je Fedora Workstation s GNOME Shell a
uživatelským systemd. Po instalaci přes Meson nebo RPM lze kontroly po ukončení
aplikace výslovně zapnout v Nastavení. ARSS vytvoří soukromé drop-in rozvrhy v
`$XDG_CONFIG_HOME/systemd/user`, zapne pouze timer druhu s nenulovým intervalem
a ponechá RSS a podcasty zcela nezávislé. Při volbě Ručně je odpovídající timer
vypnutý.

Timery používají `Persistent=true`, takže uživatelský systemd po novém
přihlášení nebo restartu zachytí zmeškanou kontrolu. ARSS automaticky
nezapíná systemd lingering, a proto po úplném odhlášení závisí běh na
nastavení uživatelského systemd daného počítače. Stav lze ověřit bez jeho
změny:

```bash
systemctl --user list-timers 'arss-monitor@*.timer'
```

Jednorázové služby spouštějí interní headless příkaz
`arss --background-check rss` nebo `podcast`. Příkaz před sítí znovu ověří
uložený opt-in, interval, poslední dokončení a mezprocesní zámek. Při vypnutí
systemd pošle běžící službě `SIGTERM`; klient uzavře aktivní odpověď a
nedokončený běh neposune checkpoint ani čas poslední kontroly. Kliknutí na
oznámení nadále aktivuje běžnou grafickou aplikaci přes D-Bus.

## RPM pro Fedoru

Lokální RPM a zdrojové SRPM se sestaví bez přístupu k síti:

```bash
sudo dnf install \
  rpm-build meson ninja-build python3-devel python3-gobject python3-requests \
  python3-setuptools \
  gtk4 libadwaita appstream desktop-file-utils \
  gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
  gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free
cd arss-linux
./tools/build-rpm.sh
```

Výsledky jsou v `dist/rpm`. Skript nejprve zkontroluje, že se shoduje verze
v Mesonu, Python balíčku, `pyproject.toml`, AppStream metadatech a RPM specu,
potom vytvoří normalizovaný zdrojový archiv v dočasném adresáři pod
`/tmp`, spustí deterministické jednotkové testy a validaci metadat a nakonec
sestaví binární RPM i SRPM. Normalizovaný archiv `arss-VERZE.tar.gz` uloží
k balíčkům, takže je možné samostatně ověřit jeho kontrolní součet.
Fedora release lze pro další sestavení stejné verze zvýšit například
příkazem:

```bash
ARSS_RPM_RELEASE=3 ./tools/build-rpm.sh
```

Jiné umístění výsledků lze zvolit pomocí `ARSS_RPM_OUTPUT`. Pro opakovatelný
obsah zdrojového archivu je výchozí `SOURCE_DATE_EPOCH` pevně svázán s
aktuálním vydáním; při nové verzi je nutné aktualizovat jej spolu s datem
vydání. Samotné RPM kontejnery nemusejí mít mezi dvěma buildy shodný SHA-256,
protože RPM 6 ukládá do SRPM expandovanou dočasnou cestu; normalizovaný
zdrojový archiv a instalovaný payload jsou však shodné.

## Testy

```bash
cd arss-linux
python3 -m unittest discover -s tests -v
```

Současná automatická sada je deterministická a nepřistupuje k síti. Samostatné
živé testy veřejných endpointů zatím v repozitáři nejsou; případné budoucí testy
musí být výslovně zapnuté pomocí `ARSS_RUN_LIVE_TESTS=1`. V grafické uživatelské
relaci lze navíc spustit `python3 tools/gui_smoke.py` a
`python3 tools/large_text_smoke.py` (22 pt, angličtina i čeština, šířka 320 px)
a `python3 tools/accessibility_smoke.py`. Před vydáním je nutné celé rozhraní
projít s Orkou, klávesnicí a ve světlém i tmavém motivu.

Fedora CI spouští stejné tři automatické GUI kontroly v izolovaném Xvfb a
D-Bus/AT-SPI sezení pomocí `tools/run-headless-smoke.sh`; nenahrazují závěrečnou
ruční kontrolu s Orkou, ale před sestavením RPM ověří skutečný accessibility
strom, ovládání, české i anglické rozložení a zvětšený text.

## Společný kontrakt platforem

Katalog televizních a rozhlasových stanic už není zapsán přímo v Pythonu.
Linux načítá ověřenou vendored kopii jazykově neutrálního ARSS Contractu,
zachovává stabilní ID napříč platformami a při prvním použití bezpečně migruje
starší providerová ID. Aktualizační nástroj nepoužívá submoduly ani síť a
plánovaný workflow pouze otevře kontrolovatelný pull request. Podrobný postup a
formát locku popisuje `docs/contract.md`.

## Přístupnost

Implementace je kontrolována proti vznikajícímu
[Linux Accessibility Development Guide](https://gitlab.gnome.org/Community/linux-a11y-dev-guide)
(auditovaný commit `a477501d3f97ffa1465a81c4a86508ce9af4ff38` z 30. července
2026).
Vznikající guide zatím nemá samostatnou kapitolu pro seznamy GTK; kontrakt
obsahových seznamů proto navíc vychází z oficiální dokumentace
[`Gtk.ListView`](https://docs.gtk.org/gtk4/class.ListView.html) a
[`Gtk.ListTabBehavior`](https://docs.gtk.org/gtk4/enum.ListTabBehavior.html).
Šipky mění položku, Enter ji aktivuje a Tab projde jen jejími vedlejšími akcemi.
GTK 4.20 je minimem mimo jiné proto, že od této verze standardní
`Gtk.DropDown` zveřejňuje asistivním technologiím vybranou položku a textovou
  hodnotu bez vlastní, neúplné implementace. Před seznamem stanic je standardní
  `Gtk.SearchEntry`, která filtruje standardní `Gtk.DropDown` podle názvů i
  kontraktových aliasů. Používá přesnou společnou normalizaci NFKD, case-folding,
  prefixy slov a shodu celé normalizované podřetězcové fráze. Klávesová cesta je
  Tab do vyhledávání, zadání výrazu, Tab do seznamu a Enter pro potvrzení.

- pouze standardní GTK ovládací prvky a explicitní role AT-SPI;
- viditelné textové akce, žádná funkce dostupná jen gestem nebo ikonou;
- sémantické nadpisy, propojené popisky formulářů a oznamované stavy;
- přepínač kontrol na pozadí nemá matoucí mnemoniku; ovládá se standardně
  Tabem, mezerníkem a Enterem;
- nativní posuvník hlasitosti 0–100 %, ovladatelný šipkami, Home a End;
- samostatné informace v detailu pořadu a prázdné RSS/podcastové stavy jsou
  jedním vybratelným textovým zastavením na Tab;
- jeden fokusovatelný a aktivovatelný `LIST_ITEM` pro zdroj, článek, epizodu, výsledek katalogu a pořad;
- `Nahoru`/`Dolů`, `Home`/`End` a `Page Up`/`Page Down` navigují uvnitř seznamu, `Enter` položku otevře;
- `Tab` navštíví jen aktuální řádek a skutečné sekundární akce, potom seznam opustí;
- klávesové přepínání hlavních karet pomocí `Alt+1` až `Alt+4`;
- minimální výška interaktivních prvků 48 px a rozložení odolné vůči velkému písmu.

## Licence

ARSS je svobodný software vydaný pod licencí GNU General Public License,
verze 3 nebo kterákoli pozdější. Přibalený katalog doporučených RSS zdrojů je
samostatně poskytován pod CC0 1.0, jak uvádí
`arss/data/rss_directory_NOTICE.txt`. Vendored kopie společného ARSS Contractu
je poskytována pod licencí MIT; její úplné znění zůstává v
`arss/data/contract/LICENSE`. Původ a CC0 licenci kanonického RSS katalogu
uvádí také `arss/data/contract/THIRD_PARTY_NOTICES.md`.
