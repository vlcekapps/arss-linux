"""Small, runtime-switchable Czech/English message catalogue.

GTK applications normally use gettext.  ARSS lets the user switch language at
runtime, though, so keeping the compact catalogue in Python avoids changing the
process locale and makes translation parity straightforward to test.
"""

from __future__ import annotations

import locale
from collections.abc import Mapping


MESSAGES: dict[str, dict[str, str]] = {
    "cs": {
        "app_subtitle": "Přístupná čtečka RSS, podcastů a programu",
        "rss": "RSS",
        "podcasts": "Podcasty",
        "guide": "TV program",
        "settings": "Nastavení",
        "rss_heading": "RSS kanály",
        "podcasts_heading": "Podcasty",
        "guide_heading": "Televizní a rozhlasový program",
        "settings_heading": "Nastavení",
        "filter_rss": "Filtrovat RSS kanály",
        "filter_podcasts": "Filtrovat podcasty",
        "open_default": "Otevřít výchozí RSS kanál",
        "default_missing": "Výchozí RSS kanál není nastaven.",
        "add_address": "Přidat adresu",
        "search_directory": "Vyhledat v katalogu",
        "import_opml": "Importovat OPML",
        "export_opml": "Exportovat OPML",
        "rss_options": "Možnosti RSS",
        "podcast_options": "Možnosti podcastů",
        "open": "Otevřít",
        "options": "Možnosti",
        "options_for": "Možnosti: {title}",
        "open_source": "Otevřít zdrojovou adresu",
        "copy_address": "Kopírovat adresu",
        "rename": "Přejmenovat",
        "set_default": "Nastavit jako výchozí",
        "delete": "Smazat",
        "default_suffix": ", výchozí",
        "empty_rss": "Žádné RSS kanály neodpovídají filtru.",
        "empty_podcasts": "Žádné podcasty neodpovídají filtru.",
        "new_rss": "Nový RSS nebo Atom kanál",
        "new_podcast": "Nový podcast",
        "feed_address": "Adresa feedu",
        "add": "Přidat",
        "cancel": "Zrušit",
        "save": "Uložit",
        "close": "Zavřít",
        "back": "Zpět",
        "retry": "Zkusit znovu",
        "loading": "Načítám…",
        "validating": "Ověřuji feed…",
        "added": "Přidáno: {title}",
        "already_exists": "Tato adresa už je přidaná.",
        "not_podcast": "Feed neobsahuje žádnou přehratelnou zvukovou epizodu.",
        "invalid_address": "Zadejte úplnou adresu začínající http:// nebo https://.",
        "error": "Chyba",
        "load_error": "Zdroj se nepodařilo načíst. {detail}",
        "save_error": "Změny se nepodařilo bezpečně uložit. Původní data zůstala zachována.",
        "store_unavailable": "Uložený seznam nelze bezpečně přečíst. Změny jsou vypnuté, aby se soubor nepřepsal. {detail}",
        "deleted": "Položka byla smazána.",
        "renamed": "Položka byla přejmenována.",
        "default_set": "{title} je nyní výchozí RSS kanál.",
        "confirm_delete": "Opravdu chcete smazat {title}?",
        "new_name": "Nový název",
        "name_required": "Nový název nesmí být prázdný.",
        "copied": "Adresa byla zkopírována do schránky.",
        "articles": "Články",
        "episodes": "Epizody",
        "articles_count": "{title} — článků: {count}",
        "episodes_count": "{title} — epizod: {count}",
        "empty_articles": "Tento kanál neobsahuje žádné články.",
        "empty_episodes": "Tento podcast neobsahuje přehratelné epizody.",
        "article_open_hint": "Otevře článek v externím prohlížeči.",
        "episode_open_hint": "Otevře interní přehrávač ARSS.",
        "unknown_article": "Nelze najít název článku.",
        "unknown_episode": "Nelze najít název epizody.",
        "information": "Informace",
        "information_for": "Informace o článku {title}",
        "copy_title_link": "Kopírovat název a odkaz",
        "directory_rss": "Katalog RSS",
        "directory_podcast": "Katalog podcastů",
        "search_phrase": "Hledaný výraz",
        "search_required": "Zadejte hledaný výraz.",
        "search": "Vyhledat",
        "searching": "Vyhledávám…",
        "search_results": "Výsledky vyhledávání",
        "empty_results": "Nebyly nalezeny žádné odpovídající zdroje.",
        "rss_directory_note": "Doporučené RSS zdroje se hledají pouze v tomto počítači.",
        "podcast_directory_note": "Vyhledávání podcastů kombinuje Podcast Index s regionálním katalogem Apple Podcasts. Hledaný výraz se odešle oběma službám; u víceslovného dotazu odešle ARSS službě Apple Podcasts také omezený počet kratších částí. Každý feed se před přidáním ověří.",
        "activate_to_add": "Aktivací položku ověříte a přidáte.",
        "opml_imported": "Importováno položek: {count}.",
        "opml_imported_with_skipped": "Importováno položek: {count}. Přeskočeno nedostupných feedů nebo položek bez přehratelných epizod: {skipped}.",
        "opml_podcasts_not_imported": "Nebyly nalezeny žádné přehratelné podcasty. Nedostupné feedy byly rovněž přeskočeny.",
        "opml_exported": "OPML bylo exportováno.",
        "opml_nothing": "Soubor neobsahuje žádné nové platné položky.",
        "choose_opml": "Vyberte OPML soubor",
        "save_opml": "Uložit OPML soubor",
        "television": "Televize",
        "radio": "Rozhlas",
        "medium": "Médium",
        "station": "Stanice",
        "station_search": "Hledat stanici",
        "station_search_hint": "Název nebo alias stanice",
        "date": "Datum",
        "date_format": "RRRR-MM-DD",
        "invalid_date": "Zadejte platné datum ve formátu RRRR-MM-DD.",
        "no_station": "Vyberte dostupnou stanici.",
        "show_program": "Zobrazit program",
        "guide_intro": (
            "Vyberte médium, stanici a datum. Stanice jsou seřazené podle "
            "společného katalogu a lze je filtrovat podle názvu nebo aliasu. "
            "Každý pořad je jediný přístupný řádek."
        ),
        "guide_source_note": "Data poskytují Česká televize, Centrum.cz, Český rozhlas a SMS.cz.",
        "program_heading": "{station} — {date}",
        "programs_count": "{station} — {date} ({count})",
        "empty_program": "Pro tuto stanici a datum není program k dispozici.",
        "guide_program_loading": "Načítám program…",
        "guide_error_title": "Program není dostupný",
        "guide_program_error": "Program se nepodařilo načíst. Zkontrolujte připojení k internetu a zkuste to znovu.",
        "program_details": "Podrobnosti pořadu",
        "now": "Právě probíhá",
        "audio_description": "Komentováno pro nevidomé",
        "audio_unknown": "Informace o komentáři pro nevidomé není v záložním zdroji dostupná.",
        "unknown_program": "Pořad bez názvu",
        "program_item": "{time}. {title}",
        "show_program_details": "Otevře podrobnosti pořadu",
        "description_missing": "Popis není k dispozici.",
        "open_program": "Otevřít stránku pořadu",
        "open_archive": "Otevřít archiv",
        "player": "Přehrávač podcastu",
        "preparing": "Připravuji zvuk…",
        "ready": "Připraveno",
        "playing": "Přehrává se",
        "paused": "Pozastaveno",
        "completed": "Přehrávání dokončeno",
        "playback_error": "Epizodu se nepodařilo přehrát.",
        "playback_error_detail": "Epizodu se nepodařilo přehrát. {detail}",
        "speed_change_error": "Rychlost přehrávání se nepodařilo změnit. {detail}",
        "play": "Přehrát",
        "pause": "Pozastavit",
        "seek_back": "Zpět o 15 sekund",
        "seek_forward": "Vpřed o 30 sekund",
        "position": "Pozice přehrávání: {position} z {duration}",
        "volume": "Hlasitost",
        "volume_percent": "{value} %",
        "playback_position": "Pozice přehrávání",
        "speed": "Rychlost přehrávání",
        "common_settings": "Společná nastavení",
        "rss_settings": "Nastavení RSS",
        "podcast_settings": "Nastavení podcastů",
        "about": "O aplikaci ARSS",
        "language": "Jazyk aplikace",
        "language_mnemonic": "_Jazyk aplikace",
        "language_system": "Podle systému",
        "language_cs": "Čeština",
        "language_en": "English",
        "language_restart": "Změna jazyka se použije po novém otevření okna.",
        "language_changed": "Jazyk aplikace byl změněn.",
        "filter_after": "Umístit filtrování za seznam",
        "background_checks": "Kontroly na pozadí",
        "background_checks_enabled_description": "Zapnuto: uživatelské časovače systemd kontrolují nové položky i po zavření ARSS.",
        "background_checks_disabled_description": "Vypnuto: automatické kontroly běží pouze tehdy, když je ARSS otevřená.",
        "background_checks_enabling": "Zapínám kontroly na pozadí…",
        "background_checks_disabling": "Vypínám kontroly na pozadí…",
        "background_checks_enabled_status": "Kontroly na pozadí jsou zapnuté.",
        "background_checks_disabled_status": "Kontroly na pozadí jsou vypnuté.",
        "background_checks_api_missing": "Tato instalace neumí spravovat kontroly na pozadí.",
        "background_checks_error": "Nastavení kontrol na pozadí se nepodařilo změnit. {detail}",
        "show_article_dates": "Zobrazovat datum článků",
        "show_episode_dates": "Zobrazovat datum epizod",
        "rss_interval": "Automatická kontrola RSS",
        "podcast_interval": "Automatická kontrola podcastů",
        "rss_interval_mnemonic": "Automatická kontrola _RSS",
        "podcast_interval_mnemonic": "Automatická kontrola _podcastů",
        "manual": "Ručně",
        "every_minutes": "Každých {minutes} minut",
        "every_hour": "Každou hodinu",
        "every_hours": "Každé {hours} hodiny",
        "checks_running_only": "Automatické kontroly běží pouze tehdy, když je ARSS spuštěná.",
        "long_date": "{day}. {month} {year}",
        "month_1": "ledna",
        "month_2": "února",
        "month_3": "března",
        "month_4": "dubna",
        "month_5": "května",
        "month_6": "června",
        "month_7": "července",
        "month_8": "srpna",
        "month_9": "září",
        "month_10": "října",
        "month_11": "listopadu",
        "month_12": "prosince",
        "help": "Nápověda",
        "help_text": (
            "Mezi hlavními částmi RSS, Podcasty, TV program a Nastavení "
            "přepínejte kartami, šipkami nebo Alt+1 až Alt+4. Enter aktivuje "
            "vybranou položku. RSS kanál otevře seznam článků; článek se vždy "
            "otevře ve výchozím externím prohlížeči. Podcast otevře seznam "
            "epizod a epizoda interní přehrávač ARSS. V přehrávači jsou "
            "Play/Pause, posun o 15 sekund zpět a 30 sekund vpřed při známé "
            "délce, hlasitost 0 až 100 % a rychlost 0,5× až 2×. Přehrávání "
            "pokračuje i při přechodu do jiného okna a skončí zavřením "
            "přehrávače nebo ARSS. V TV programu vyberte "
            "televizi nebo rádio, stanici a datum ve formátu RRRR-MM-DD. "
            "Stanice jsou seřazené podle společného katalogu. Do pole Hledat "
            "stanici napište začátky slov z názvu nebo aliasu a z výsledků "
            "vyberte požadovanou stanici. Potom zvolte Zobrazit program a aktivací pořadu "
            "otevřete jeho podrobnosti. "
            "Tlačítko Možnosti u odběru umožňuje otevřít nebo kopírovat zdrojovou "
            "adresu, položku přejmenovat či smazat a RSS kanál nastavit jako "
            "výchozí. Menu Možnosti RSS a Možnosti podcastů obsahují ruční přidání "
            "adresy, hledání v katalogu a import nebo export OPML. Automatické "
            "kontroly RSS a podcastů se nastavují odděleně. Ručně "
            "znamená načtení nových položek po otevření odběru. Bez zapnutých "
            "Kontrol na pozadí běží zvolené intervaly jen při otevřené aplikaci; "
            "po výslovném zapnutí je po zavření ARSS zajišťují uživatelské "
            "časovače systemd. Nové položky přicházejí jako akční oznámení "
            "GNOME. Systémový zvuk oznámení řídí výhradně prostředí GNOME; "
            "ARSS vlastní zvuk nepřehrává."
        ),
        "version": "Verze {version}",
        "thank_author": "Poděkování autorovi",
        "status_ready": "Připraveno.",
        "preferences_broken": "Nastavení nelze přečíst",
        "preferences_broken_detail": "Soubor nastavení je poškozený nebo nedostupný. ARSS jej nepřepíše bez vašeho potvrzení.",
        "backup_reset": "Zálohovat a obnovit výchozí",
        "recovery_failed": "Nastavení se nepodařilo zazálohovat a obnovit. {detail}",
        "new_article_one": "1 nový článek",
        "new_articles": "Nové články: {count}",
        "new_episode_one": "1 nová epizoda",
        "new_episodes": "Nové epizody: {count}",
        "notification_body": "Nový obsah zobrazíte otevřením aplikace ARSS.",
        "notification_single_body": "{source}: {item}",
    },
    "en": {
        "app_subtitle": "Accessible RSS, podcast and programme reader",
        "rss": "RSS",
        "podcasts": "Podcasts",
        "guide": "TV guide",
        "settings": "Settings",
        "rss_heading": "RSS feeds",
        "podcasts_heading": "Podcasts",
        "guide_heading": "Television and radio guide",
        "settings_heading": "Settings",
        "filter_rss": "Filter RSS feeds",
        "filter_podcasts": "Filter podcasts",
        "open_default": "Open the default RSS feed",
        "default_missing": "The default RSS feed is not set.",
        "add_address": "Add address",
        "search_directory": "Search directory",
        "import_opml": "Import OPML",
        "export_opml": "Export OPML",
        "rss_options": "RSS options",
        "podcast_options": "Podcast options",
        "open": "Open",
        "options": "Options",
        "options_for": "Options: {title}",
        "open_source": "Open source address",
        "copy_address": "Copy address",
        "rename": "Rename",
        "set_default": "Set as default",
        "delete": "Delete",
        "default_suffix": ", default",
        "empty_rss": "No RSS feeds match the filter.",
        "empty_podcasts": "No podcasts match the filter.",
        "new_rss": "New RSS or Atom feed",
        "new_podcast": "New podcast",
        "feed_address": "Feed address",
        "add": "Add",
        "cancel": "Cancel",
        "save": "Save",
        "close": "Close",
        "back": "Back",
        "retry": "Try again",
        "loading": "Loading…",
        "validating": "Checking the feed…",
        "added": "Added: {title}",
        "already_exists": "This address has already been added.",
        "not_podcast": "The feed does not contain a playable audio episode.",
        "invalid_address": "Enter a complete address beginning with http:// or https://.",
        "error": "Error",
        "load_error": "The source could not be loaded. {detail}",
        "save_error": "The changes could not be stored safely. Existing data was preserved.",
        "store_unavailable": "The saved list cannot be read safely. Changes are disabled so the file is not overwritten. {detail}",
        "deleted": "The item was deleted.",
        "renamed": "The item was renamed.",
        "default_set": "{title} is now the default RSS feed.",
        "confirm_delete": "Do you really want to delete {title}?",
        "new_name": "New name",
        "name_required": "The new name must not be empty.",
        "copied": "The address was copied to the clipboard.",
        "articles": "Articles",
        "episodes": "Episodes",
        "articles_count": "{title} — articles: {count}",
        "episodes_count": "{title} — episodes: {count}",
        "empty_articles": "This feed contains no articles.",
        "empty_episodes": "This podcast contains no playable episodes.",
        "article_open_hint": "Opens the article in an external browser.",
        "episode_open_hint": "Opens the internal ARSS player.",
        "unknown_article": "The article title is unavailable.",
        "unknown_episode": "The episode title is unavailable.",
        "information": "Information",
        "information_for": "Information about article {title}",
        "copy_title_link": "Copy title and link",
        "directory_rss": "RSS directory",
        "directory_podcast": "Podcast directory",
        "search_phrase": "Search phrase",
        "search_required": "Enter a search phrase.",
        "search": "Search",
        "searching": "Searching…",
        "search_results": "Search results",
        "empty_results": "No matching sources were found.",
        "rss_directory_note": "Recommended RSS sources are searched only on this computer.",
        "podcast_directory_note": "Podcast search combines Podcast Index with the regional Apple Podcasts directory. The search phrase is sent to both services; for a multi-word query, ARSS also sends Apple Podcasts a limited number of shorter parts. Every feed is checked before it is added.",
        "activate_to_add": "Activate to check and add the item.",
        "opml_imported": "Items imported: {count}.",
        "opml_imported_with_skipped": "Items imported: {count}. Unavailable feeds or entries without playable episodes skipped: {skipped}.",
        "opml_podcasts_not_imported": "No playable podcasts were found. Unavailable feeds were also skipped.",
        "opml_exported": "The OPML file was exported.",
        "opml_nothing": "The file contains no new valid items.",
        "choose_opml": "Choose an OPML file",
        "save_opml": "Save the OPML file",
        "television": "Television",
        "radio": "Radio",
        "medium": "Medium",
        "station": "Station",
        "station_search": "Search stations",
        "station_search_hint": "Station name or alias",
        "date": "Date",
        "date_format": "YYYY-MM-DD",
        "invalid_date": "Enter a valid date in YYYY-MM-DD format.",
        "no_station": "Select an available station.",
        "show_program": "Show programme",
        "guide_intro": (
            "Choose a medium, station and date. Stations follow the shared "
            "catalogue order and can be filtered by name or alias. Every "
            "programme is one accessible row."
        ),
        "guide_source_note": "Data is provided by Czech Television, Centrum.cz, Czech Radio and SMS.cz.",
        "program_heading": "{station} — {date}",
        "programs_count": "{station} — {date} ({count})",
        "empty_program": "No programme is available for this station and date.",
        "guide_program_loading": "Loading programme…",
        "guide_error_title": "Programme unavailable",
        "guide_program_error": "The programme could not be loaded. Check the internet connection and try again.",
        "program_details": "Programme details",
        "now": "On now",
        "audio_description": "Audio described",
        "audio_unknown": "Audio-description information is unavailable from the fallback source.",
        "unknown_program": "Untitled programme",
        "program_item": "{time}. {title}",
        "show_program_details": "Opens programme details",
        "description_missing": "A description is not available.",
        "open_program": "Open programme page",
        "open_archive": "Open archive",
        "player": "Podcast player",
        "preparing": "Preparing audio…",
        "ready": "Ready",
        "playing": "Playing",
        "paused": "Paused",
        "completed": "Playback completed",
        "playback_error": "This episode could not be played.",
        "playback_error_detail": "This episode could not be played. {detail}",
        "speed_change_error": "The playback speed could not be changed. {detail}",
        "play": "Play",
        "pause": "Pause",
        "seek_back": "Back 15 seconds",
        "volume": "Volume",
        "volume_percent": "{value}%",
        "seek_forward": "Forward 30 seconds",
        "position": "Playback position: {position} of {duration}",
        "playback_position": "Playback position",
        "speed": "Playback speed",
        "common_settings": "Common settings",
        "rss_settings": "RSS settings",
        "podcast_settings": "Podcast settings",
        "about": "About ARSS",
        "language": "Application language",
        "language_mnemonic": "Application _language",
        "language_system": "Follow system",
        "language_cs": "Čeština",
        "language_en": "English",
        "language_restart": "The language change is applied when the window is opened again.",
        "language_changed": "The application language was changed.",
        "filter_after": "Place filtering after the list",
        "background_checks": "Background checks",
        "background_checks_enabled_description": "On: systemd user timers check for new items even after ARSS is closed.",
        "background_checks_disabled_description": "Off: automatic checks run only while ARSS is open.",
        "background_checks_enabling": "Enabling background checks…",
        "background_checks_disabling": "Disabling background checks…",
        "background_checks_enabled_status": "Background checks are enabled.",
        "background_checks_disabled_status": "Background checks are disabled.",
        "background_checks_api_missing": "This installation cannot manage background checks.",
        "background_checks_error": "The background-check setting could not be changed. {detail}",
        "show_article_dates": "Show article dates",
        "show_episode_dates": "Show episode dates",
        "rss_interval": "Automatic RSS check",
        "podcast_interval": "Automatic podcast check",
        "rss_interval_mnemonic": "Automatic _RSS check",
        "podcast_interval_mnemonic": "Automatic _podcast check",
        "manual": "Manually",
        "every_minutes": "Every {minutes} minutes",
        "every_hour": "Every hour",
        "every_hours": "Every {hours} hours",
        "checks_running_only": "Automatic checks run only while ARSS is open.",
        "long_date": "{month} {day}, {year}",
        "month_1": "January",
        "month_2": "February",
        "month_3": "March",
        "month_4": "April",
        "month_5": "May",
        "month_6": "June",
        "month_7": "July",
        "month_8": "August",
        "month_9": "September",
        "month_10": "October",
        "month_11": "November",
        "month_12": "December",
        "help": "Help",
        "help_text": (
            "Switch among RSS, Podcasts, TV guide and Settings with the tabs, "
            "arrow keys or Alt+1 through Alt+4. Enter activates the selected item. "
            "An RSS feed opens its article list; an article always opens in the "
            "default external browser. A podcast opens its episode list and an "
            "episode opens the internal ARSS player. The player provides Play/Pause, "
            "15 seconds back and 30 seconds forward when the duration is known, "
            "volume from 0 to 100%, and speeds from 0.5× to 2×. Playback "
            "continues when you switch to another window and stops when the "
            "player or ARSS closes. In TV guide, choose television "
            "or radio, a station and a date in YYYY-MM-DD format. Television "
            "stations follow the shared catalogue order. In Search stations, "
            "type word prefixes from a station name or alias, then choose the "
            "wanted station from the results. Then "
            "select Show programme and activate a programme to open its details. "
            "The Options "
            "button for a subscription can open or copy its source address, rename "
            "or delete it, and set an RSS feed as the default. The RSS options and "
            "Podcast options menus contain manual address entry, directory search, "
            "and OPML import or export. Automatic RSS "
            "and podcast checks are configured independently. Manually means new "
            "items are loaded when you open a subscription. Without Background "
            "checks, selected intervals run only while the application is open; "
            "after explicit opt-in, systemd user timers continue them after ARSS "
            "closes. New items arrive as actionable GNOME notifications. GNOME "
            "exclusively controls the system notification sound; ARSS does not "
            "play a separate sound."
        ),
        "version": "Version {version}",
        "thank_author": "Thank the author",
        "status_ready": "Ready.",
        "preferences_broken": "Settings cannot be read",
        "preferences_broken_detail": "The settings file is corrupt or unavailable. ARSS will not overwrite it without your confirmation.",
        "backup_reset": "Back up and restore defaults",
        "recovery_failed": "The settings could not be backed up and restored. {detail}",
        "new_article_one": "1 new article",
        "new_articles": "New articles: {count}",
        "new_episode_one": "1 new episode",
        "new_episodes": "New episodes: {count}",
        "notification_body": "Open ARSS to view the new content.",
        "notification_single_body": "{source}: {item}",
    },
}


def system_language() -> str:
    """Return one of the supported language tags for the current process."""

    current = locale.getlocale()[0] or locale.getdefaultlocale()[0] or "en"
    return "cs" if current.replace("_", "-").lower().startswith("cs") else "en"


class Translator:
    """Format messages from an explicitly selected or system language."""

    def __init__(self, language: str = "system") -> None:
        self.requested_language = language if language in {"system", "cs", "en"} else "system"
        self.language = system_language() if self.requested_language == "system" else self.requested_language

    @property
    def messages(self) -> Mapping[str, str]:
        return MESSAGES[self.language]

    def __call__(self, key: str, **values: object) -> str:
        return self.messages[key].format(**values)


def assert_catalogue_parity() -> None:
    """Raise a useful error when a translation was added to only one locale."""

    reference = set(MESSAGES["cs"])
    for language, messages in MESSAGES.items():
        missing = reference - set(messages)
        extra = set(messages) - reference
        if missing or extra:
            raise AssertionError(f"Translation mismatch for {language}: missing={missing}, extra={extra}")
