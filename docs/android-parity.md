# Funkční parita s ARSS pro Android

Tento dokument uzavírá přenos uživatelských funkcí z ARSS 1.6.4 pro Android
(revize `18721c5385600279f486e1b30b7bed3a95b8326c`) do nativní aplikace pro
Fedora Linux. Od následujícího vydání se Linux varianta vyvíjí samostatně pro
desktop GNOME; mobilní způsob provedení se nepovažuje za závazný tam, kde má
GNOME vlastní standardní rozhraní.

## Matice funkcí

| Oblast Android 1.6.4 | Implementace pro Fedora/GNOME | Stav |
| --- | --- | --- |
| Čtyři hlavní karty RSS, Podcasty, TV program a Nastavení | `Adw.ViewStack` a `Adw.ViewSwitcher`, šipky, Tab a `Alt+1` až `Alt+4` | Hotovo |
| Výchozí RSS kanál, filtrování odběrů a volitelná poloha filtru | Stejné preference a pravidlo shody všech normalizovaných slov | Hotovo |
| Přidání a síťové ověření RSS/Atom kanálu nebo podcastu | Samostatné přístupné formuláře; podcast musí mít přehratelný zvuk | Hotovo |
| Přejmenování, smazání, kopírování a otevření adresy, nastavení výchozího RSS | Viditelné klávesnicové menu Možnosti u každého odběru | Hotovo |
| Přidání adresy, katalog a import/export OPML | Akce jsou v menu Možnosti RSS nebo Možnosti podcastů; odmítnuté podcasty se započítají | Hotovo |
| Lokální katalog 118 RSS zdrojů | Stejný auditovaný OPML katalog, hledání probíhá offline | Hotovo |
| Podcast Index a regionální Apple Podcasts | Veřejný Podcast Index plus Apple; volitelně podepsané API s klíčem a tajemstvím | Hotovo |
| RSS 2.0, Atom 1.0, enclosure, Media RSS, iTunes duration a TN.cz | Stejné formáty, řazení a bezpečnostní limity, včetně regresních fixture testů | Hotovo |
| Pět prvotních RSS kanálů podle jazyka, pouze jednou | Stejné české a anglické sady a trvalý příznak inicializace | Hotovo |
| Seznam článků a externí webový prohlížeč | Jeden fokusovatelný ovládací prvek na článek; žádný WebView | Hotovo |
| Seznam epizod a interní přehrávač | Play/Pause, −15 s, +30 s, posuv při známé délce, hlasitost 0–100 % a rychlosti 0,5× až 2× | Hotovo |
| Pozastavení přehrávání po opuštění přehrávače/aplikace | Desktopová verze při ztrátě aktivity pokračuje; končí zavřením přehrávače/aplikace a reaguje jen na skutečné audio přerušení | Záměrná desktopová odchylka |
| TV a rozhlas podle média, stanice a data | Stejné katalogy, zdroje, fallbacky a časová zóna Praha; TV stanice jsou souvisle seřazené podle vysílatele a nativně prohledávatelné | Hotovo |
| Nova Sport 1–6, Oneplay Sport, ČRo, SRO, komerční rádia | Stejné stabilní katalogy a zdrojové identifikátory | Hotovo |
| Detail pořadu, právě vysílá, odkazy a zvukový popis `AD`/`AD?` | Stejná významová pravidla, včetně neznámého stavu fallbacku | Hotovo |
| Volitelné datum článků a epizod | Samostatné přepínače v Nastavení | Hotovo |
| Čeština, angličtina a jazyk systému | Volba se projeví okamžitě vytvořením nového hlavního okna | Hotovo |
| Nápověda, verze a Poděkování autorovi | Přístupné obrazovky a přesný autorský odkaz | Hotovo |
| Nezávislé intervaly RSS/podcastů od 1 minuty do 12 hodin | Stejné intervaly, v otevřené aplikaci přes GLib; po opt-in přes uživatelský systemd | Hotovo |
| První kontrola bez záplavy starých oznámení a omezené checkpointy | Sdílené, atomické XDG checkpointy pro ruční i automatické načtení | Hotovo |
| Seskupená oznámení, nejvýše jeden vlastní zvuk na dávku | Stabilní položkové notifikace a souhrn jen pro více položek přes `GNotification`; zvuk vybírá GNOME | Záměrná desktopová odchylka |
| Systémový, tichý, RSS a Alert 1–4 zvlášť pro RSS/podcasty | Vlastní volby a ukázky jsou odstraněné; veškerou zvukovou signalizaci oznámení řídí GNOME | Záměrná desktopová odchylka |
| Kompatibilní `readFeeds.opml` a `podcasts.opml` | Soubory zůstávají kompatibilní a zapisují se atomicky pod XDG data | Hotovo |
| Úplná práce z klávesnice a čtečky obrazovky | Standardní GTK role, názvy, nadpisy, stavy, živá hlášení a jediný účelný Tab stop | Hotovo |

## Záměrné desktopové ekvivalenty

Následující rozdíly nejsou chybějící funkce, ale nahrazení mobilního systémového
rozhraní standardem Fedory a GNOME:

- spodní/postranní mobilní navigaci nahrazuje přístupný přepínač pohledů v
  záhlaví; všechny čtyři karty mají jméno, stav vybráno a klávesovou zkratku;
- dlouhý stisk a vlastní Android accessibility akce nahrazuje viditelné menu
  Možnosti, které funguje z klávesnice i s Orcou;
- Android AlarmManager, JobScheduler a oprávnění přesných alarmů nahrazují
  uživatelské systemd timery; instalace je nikdy sama nezapne a každý headless
  běh znovu ověří uložený souhlas;
- Android audio focus nahrazuje kooperativní politika standardních rolí
  PipeWire/WirePlumber; GNOME media klávesy obsluhuje MPRIS;
- oprávnění, seskupení, zvuk a obsah na zamčené obrazovce řídí na desktopu
  GNOME Shell. ARSS používá pouze veřejné `GNotification` API a nezaručuje
  chování, které toto API neumí vyjádřit;
- aplikační data, nastavení a stav používají odpovídající adresáře
  `XDG_DATA_HOME`, `XDG_CONFIG_HOME` a `XDG_STATE_HOME`.

Za funkční mezeru se nadále považuje každá uživatelská operace z první tabulky,
která by nebyla dosažitelná klávesnicí nebo by neměla srozumitelný název a roli
pro Orcu. Taková regrese blokuje nové RPM.
