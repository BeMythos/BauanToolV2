import argparse
import os
import re
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed


def default_workers():
    """Laptop-schonende Voreinstellung: nur die Haelfte der Kerne nutzen, mind. 2.

    So bleiben Kerne fuer das Betriebssystem/andere Programme frei und der Rechner
    wird waehrend der Suche nicht komplett zaeh.
    """
    cpu = os.cpu_count() or 4
    return max(2, cpu // 2)


def _lower_priority():
    """Setzt die Prioritaet des aktuellen (Worker-)Prozesses herab.

    Dadurch laeuft die Suche im Hintergrund: sie nutzt freie CPU-Zeit, tritt aber
    hinter interaktive Programme zurueck - der Laptop bleibt bedienbar. Fehler
    werden bewusst ignoriert (die Suche funktioniert auch ohne Prioritaetswechsel).
    """
    try:
        if sys.platform.startswith("win"):
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
        else:
            os.nice(10)
    except Exception:
        pass

# Wichtig: Dieses Tool oeffnet alle Quelldateien/ZIPs ausschliesslich lesend (rb / ZipFile-Modus "r").
# Im durchsuchten Datenbestand wird nichts geschrieben, umbenannt, verschoben oder geloescht.
# Es gibt keinen Schreibzugriff irgendeiner Art - die Ergebnisse erscheinen ausschliesslich auf der Konsole.

# Wurzelverzeichnis: bewusst auf den obersten Ordner gesetzt, damit nicht mehr
# manuell ein (potenziell falscher) Unterordner ausgewaehlt werden muss. Die
# rekursive Suche deckt automatisch alle Unterordner (H, Prov, SLS, Tecis, ...) ab.
DEFAULT_ROOT_DIRECTORY = r"C:\Users\mbakhtar\OneDrive - Swiss Life Deutschland\Informationslogistik & BiPRO-Services - DQ\17_GDV\Bestandsdaten_XMLOrdner"

# Bisherige Suchstrings zur Referenz:
# "440007990", "440009381", "440011449", "440009554"
DEFAULT_SEARCH_STRING = "98000162790"

# Optionaler Jahresfilter, fest im Code: Unterordner mit Jahres-Praefix kleiner als
# dieser Wert werden uebersprungen ('Archiv' bleibt davon unberuehrt). None = alles
# durchsuchen (kein Filter). Kann weiterhin pro Aufruf mit --ab-jahr ueberschrieben werden.
DEFAULT_SINCE_YEAR = 2026

# Optionaler Vertrieb-Filter: Nur diese Unterordner der Jahres-Ordner werden durchsucht
# (z.B. ["prov", "tecis"]). None = alle Vertriebe (horbach, prov, sls, tecis, ...).
# Kann weiterhin pro Aufruf mit --vertrieb ueberschrieben werden.
DEFAULT_VERTRIEBE = ["Prov", "Proventus"]

# Optionaler Konzern-Filter: Nur ZIPs/Dateien, deren Dateiname einen dieser Begriffe
# enthaelt, werden gelesen (z.B. ["nuernberger"] oder ["talanx", "hdi"]). Gross-/
# Kleinschreibung egal, Teilstring genuegt. Leere Liste = alle Konzerne.
# Ueberschreibbar mit --konzern.
DEFAULT_KONZERN_FILTER = []

# Zusaetzliche UND-Pflichtbegriffe im Dateiinhalt: eine Datei zaehlt nur dann als
# Treffer, wenn sie ALLE diese Begriffe enthaelt (zusaetzlich zur Nummer). Leere
# Liste = keine Zusatzbedingung. Ueberschreibbar mit --and.
DEFAULT_AND_TERMS = []

# Diese Lieferungen werden IMMER durchsucht - unabhaengig vom Konzern-Filter, weil sie
# den Gesamtbestand ueber alle Konzerne hinweg enthalten koennen. Vergleich case-insensitive.
ALWAYS_INCLUDE_PATTERNS = ("aenderungsbestandlieferung", "stichtagsbestandlieferung")

# Format-Deduplication: Pro Lieferung (gleicher Basis-Dateiname) wird nur ein Format gelesen.
# Prioritaet (erster Treffer gewinnt): gdv.xml > bipro.xml > cleaned.
# Pruefsummen-Dateien (.chk.done) werden immer uebersprungen.
FORMAT_PRIORITY = ("_gdv.xml.done", "_bipro.xml.done", ".cleaned.done")
FORMAT_SKIP_SUFFIX = "_bipro.xml.chk.done"

# Suchkonfiguration, in jedem Worker gesetzt. Aufbau (siehe make_matchers):
#   needles   : Liste von (original, such_key, (byte_varianten...))
#   and_terms : Liste von (such_key, (byte_varianten...))
#   ignore_case, prefilter (bool)
# Bei Threads teilen sich alle denselben Prozess -> _MATCHERS wird einmal gesetzt.
# Bei Prozessen setzt _init_worker die Konfiguration je Kindprozess.
_MATCHERS = ([], [], False, False)


def _byte_variants(s):
    """Alle Byte-Darstellungen eines Suchbegriffs in den Kodierungen, in die auch
    _safe_decode dekodiert (utf-8, cp1252, latin-1). Fuer den verlustfreien Vorfilter.
    """
    out, seen = [], set()
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            b = s.encode(enc)
        except Exception:
            continue
        if b not in seen:
            seen.add(b)
            out.append(b)
    return tuple(out)


def make_matchers(terms, and_terms, ignore_case):
    """Baut die Suchkonfiguration inkl. Byte-Varianten fuer den Vorfilter.

    Bei ignore_case ist ein Byte-Vorfilter nicht sinnvoll (Gross/klein laesst sich
    auf Bytes nicht guenstig vergleichen) -> dann wird jede Datei voll dekodiert.
    """
    prefilter = not ignore_case
    n = [(t, (t.lower() if ignore_case else t), _byte_variants(t) if prefilter else ()) for t in terms]
    a = [((x.lower() if ignore_case else x), _byte_variants(x) if prefilter else ()) for x in and_terms]
    return (n, a, ignore_case, prefilter)


def _init_worker(matchers):
    global _MATCHERS
    _MATCHERS = matchers
    _lower_priority()


def _scan_data(data, location):
    """Durchsucht die Rohbytes einer Datei.

    Verlustfreier Vorfilter (nur wenn NICHT ignore_case): dekodiert die Datei nur,
    wenn mind. ein Suchbegriff - in irgendeiner der Kodierungen - als Bytes vorkommt
    und alle UND-Pflichtbegriffe moeglich sind. Dateien ohne Kandidaten werden gar
    nicht erst zu Text expandiert -> kein RAM-Ausschlag, kaum CPU. Anschliessend
    entscheidet die exakte Textsuche pro Zeile (faengt seltene Byte-Zufallstreffer ab).
    """
    needles, and_terms, ignore_case, prefilter = _MATCHERS

    if prefilter:
        for _key, variants in and_terms:
            if not any(v in data for v in variants):
                return []
        if not any(any(v in data for v in variants) for _o, _k, variants in needles):
            return []

    text = _safe_decode(data)

    if and_terms:
        hay_text = text.lower() if ignore_case else text
        for key, _variants in and_terms:
            if key not in hay_text:
                return []

    hits = []
    for line in text.splitlines():
        hay = line.lower() if ignore_case else line
        for orig, key, _variants in needles:
            if key in hay:
                hits.append((orig, location, line.strip()))
    return hits


def _safe_decode(data):
    """Dekodiert Rohbytes verlustfrei zu Text.

    GDV-/BiPRO-Dateien liegen mal in UTF-8, mal in cp1252/latin-1 vor. Reihenfolge:
    UTF-8 (strikt) -> cp1252 -> latin-1. latin-1 kann jedes Byte abbilden und schlaegt
    nie fehl, daher gehen - anders als bei errors="ignore" - keine Umlaute verloren.
    """
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")  # theoretisch unerreichbar


def scan_path(path):
    """Worker-Funktion: durchsucht eine einzelne Datei oder ein ZIP-Archiv komplett.

    Liest je Datei/ZIP-Eintrag einmal die Rohbytes und gibt sie an _scan_data.
    Der Speicher wird pro Eintrag wieder frei (data wird neu zugewiesen), sodass
    auch bei grossen ZIPs immer nur ein Eintrag gleichzeitig im RAM liegt.
    """
    hits = []
    errors = []
    bytes_read = 0
    if path.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    try:
                        data = zf.read(info)
                    except Exception as e:
                        errors.append(f"{path}::{info.filename}: {e}")
                        continue
                    bytes_read += len(data)
                    hits.extend(_scan_data(data, f"{path}::{info.filename}"))
                    del data
        except Exception as e:
            errors.append(f"{path} (ZIP nicht lesbar): {e}")
    else:
        try:
            with open(path, "rb") as f:
                data = f.read()
            bytes_read += len(data)
            hits.extend(_scan_data(data, path))
        except Exception as e:
            errors.append(f"{path}: {e}")
    return hits, errors, bytes_read


YEAR_FOLDER_RE = re.compile(r"^(\d{4})_")


def _folder_year(name):
    """Liest das Jahr aus Ordnernamen wie '2024_07' oder '2025_04-05'. None, wenn kein Jahres-Praefix."""
    m = YEAR_FOLDER_RE.match(name)
    return int(m.group(1)) if m else None


def _deduplicate_done_files(files):
    """Pro Lieferung (gleicher Basis-Dateiname) wird nur ein Format behalten.

    Prioritaet: _gdv.xml.done > _bipro.xml.done > .cleaned.done
    Pruefsummen-Dateien (_bipro.xml.chk.done) werden immer herausgefiltert.
    Dateien ohne bekanntes Format-Suffix (z.B. ZIPs, andere) bleiben unveraendert erhalten.
    """
    groups = {}   # base -> {suffix: filename}
    other = []    # Dateien ohne bekanntes Format-Suffix

    for f in files:
        if f.endswith(FORMAT_SKIP_SUFFIX):
            continue
        matched = False
        for suffix in FORMAT_PRIORITY:
            if f.endswith(suffix):
                base = f[: -len(suffix)]
                groups.setdefault(base, {})[suffix] = f
                matched = True
                break
        if not matched:
            other.append(f)

    result = list(other)
    for variants in groups.values():
        for suffix in FORMAT_PRIORITY:
            if suffix in variants:
                result.append(variants[suffix])
                break
    return result


def _konzern_match(filename, konzern_filters_lower):
    """True, wenn die Datei gescannt werden soll.

    - Aenderungs-/Stichtags-Lieferungen werden IMMER gescannt (Gesamtbestand).
    - Sonst nur, wenn der Dateiname mindestens einen Konzern-Begriff (Teilstring,
      case-insensitive) enthaelt. Ohne Konzern-Filter (leere Liste) wird alles gescannt.
    """
    if not konzern_filters_lower:
        return True
    name_lower = filename.lower()
    for pat in ALWAYS_INCLUDE_PATTERNS:
        if pat in name_lower:
            return True
    for k in konzern_filters_lower:
        if k in name_lower:
            return True
    return False


def gather_paths(root, since_year=None, vertriebe=None, konzern_filters=None):
    """Sammelt alle zu durchsuchenden Pfade rekursiv (root kann auch direkt eine Datei/ZIP sein).

    since_year      — filtert direkte Unterordner von root anhand ihres Jahres-Praefix.
                      Ordner ohne Jahres-Praefix (z.B. 'Archiv') werden immer mitgenommen.
    vertriebe       — filtert Unterordner der Jahres-Ordner (z.B. ['prov', 'tecis']).
                      Gross-/Kleinschreibung egal. None = alle Vertriebe.
    konzern_filters — filtert Dateien anhand des Dateinamens (Konzern-Begriff als Teilstring).
                      Aenderungs-/Stichtags-Lieferungen bleiben immer enthalten.
                      Wird VOR dem Lesen angewandt -> spart echtes I/O.
    Zusaetzlich werden pro Verzeichnis redundante Datei-Formate dedupliziert
    (gdv.xml bevorzugt, Pruefsummen-Dateien immer uebersprungen).
    """
    if os.path.isfile(root):
        return [root]

    root_abs = os.path.abspath(root)
    vertriebe_lower = {v.lower() for v in vertriebe} if vertriebe else None
    konzern_lower = [k.lower() for k in konzern_filters] if konzern_filters else []
    paths = []

    for subdir, dirs, files in os.walk(root):
        subdir_abs = os.path.abspath(subdir)
        parent_abs = os.path.abspath(os.path.dirname(subdir))

        # Jahres-Filter: direkte Kinder von root mit YYYY_-Praefix
        if since_year is not None and subdir_abs == root_abs:
            dirs[:] = [d for d in dirs if (_folder_year(d) is None or _folder_year(d) >= since_year)]

        # Vertrieb-Filter: direkte Kinder der Jahres-Ordner
        if vertriebe_lower is not None and parent_abs == root_abs and _folder_year(os.path.basename(subdir)) is not None:
            dirs[:] = [d for d in dirs if d.lower() in vertriebe_lower]

        for file in _deduplicate_done_files(files):
            if not _konzern_match(file, konzern_lower):
                continue
            paths.append(os.path.join(subdir, file))

    return paths


def load_terms(args):
    terms = list(args.numbers) if args.numbers else [DEFAULT_SEARCH_STRING]
    if args.terms_file:
        with open(args.terms_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    terms.append(line)
    seen = set()
    unique_terms = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique_terms.append(t)
    return unique_terms


def main():
    parser = argparse.ArgumentParser(
        description="Durchsucht rekursiv einen Ordner (inkl. ZIP-Inhalte) nach einer oder mehreren Versicherungsnummern."
    )
    parser.add_argument(
        "root", nargs="?", default=DEFAULT_ROOT_DIRECTORY,
        help=f"Wurzelordner, ab dem gesucht werden soll (Default: {DEFAULT_ROOT_DIRECTORY})",
    )
    parser.add_argument(
        "numbers", nargs="*",
        help=f"Eine oder mehrere zu suchende Nummern/Strings (Default: {DEFAULT_SEARCH_STRING})",
    )
    parser.add_argument(
        "--terms-file", dest="terms_file",
        help="Textdatei mit einer Nummer pro Zeile (zusaetzlich zu numbers)",
    )
    parser.add_argument(
        "--workers", type=int, default=default_workers(),
        help=(
            "Anzahl paralleler Prozesse (Default: halbe CPU-Kerne, mind. 2 - "
            "laptop-schonend). Mehr = schneller, aber Rechner wird zaeher."
        ),
    )
    parser.add_argument(
        "--ab-jahr", dest="since_year", type=int, default=DEFAULT_SINCE_YEAR,
        help=(
            "Optional: nur Unterordner ab diesem Jahr durchsuchen (z.B. 2024 fuer "
            "'2024_07', '2025_01-02', ...). Ordner ohne Jahres-Praefix wie 'Archiv' "
            f"werden immer mitdurchsucht. Standard: {DEFAULT_SINCE_YEAR} (siehe DEFAULT_SINCE_YEAR oben im Code)."
        ),
    )
    parser.add_argument(
        "--vertrieb", dest="vertriebe", nargs="+", default=DEFAULT_VERTRIEBE,
        metavar="VERTRIEB",
        help=(
            "Optional: nur diese Vertrieb-Unterordner durchsuchen (z.B. --vertrieb prov tecis). "
            f"Standard: {DEFAULT_VERTRIEBE} (siehe DEFAULT_VERTRIEBE oben im Code)."
        ),
    )
    parser.add_argument(
        "--konzern", dest="konzern_filters", nargs="*", default=DEFAULT_KONZERN_FILTER,
        metavar="KONZERN",
        help=(
            "Optional: nur Dateien lesen, deren Dateiname einen dieser Konzern-Begriffe "
            "enthaelt (Teilstring, Gross-/Kleinschreibung egal), z.B. --konzern nuernberger. "
            "AenderungsBestandLieferung und StichtagsBestandLieferung werden IMMER durchsucht. "
            f"Standard: {DEFAULT_KONZERN_FILTER or 'alle Konzerne'}."
        ),
    )
    parser.add_argument(
        "--and", dest="and_terms", nargs="*", default=DEFAULT_AND_TERMS,
        metavar="BEGRIFF",
        help=(
            "Optional: UND-Pflichtbegriffe. Eine Datei zaehlt nur als Treffer, wenn sie "
            "ALLE diese Begriffe UND die gesuchte Nummer enthaelt (z.B. --and Rueckkaufswert 2025). "
            f"Standard: {DEFAULT_AND_TERMS or 'keine Zusatzbedingung'}."
        ),
    )
    parser.add_argument(
        "--ignore-case", dest="ignore_case", action="store_true",
        help="Gross-/Kleinschreibung bei Nummern und UND-Begriffen ignorieren (Default: exakt).",
    )
    args = parser.parse_args()

    terms = load_terms(args)
    if not os.path.exists(args.root):
        parser.error(f"Pfad existiert nicht: {args.root}")

    ic = args.ignore_case
    matchers = make_matchers(terms, args.and_terms, ic)

    print(f"Suche in: {args.root}")
    print(f"Suchbegriffe: {', '.join(terms)}")
    if args.since_year is not None:
        print(f"Jahresfilter: nur Unterordner ab {args.since_year} (z.B. 'Archiv' wird trotzdem mitdurchsucht)")
    if args.vertriebe:
        print(f"Vertrieb-Filter: {', '.join(args.vertriebe)}")
    if args.konzern_filters:
        print(f"Konzern-Filter: {', '.join(args.konzern_filters)}  (Aenderungs-/Stichtags-Lieferungen immer inklusive)")
    if args.and_terms:
        print(f"UND-Pflichtbegriffe: {', '.join(args.and_terms)}")
    if ic:
        print("Gross-/Kleinschreibung: ignoriert")

    start = time.time()
    paths = gather_paths(args.root, args.since_year, args.vertriebe, args.konzern_filters)
    total = len(paths)
    print(f"{total} Dateien/Archive gefunden. Starte Suche mit {args.workers} Prozessen...")

    all_hits = []
    all_errors = []
    found_locations = set()
    processed = 0
    total_bytes = 0
    last_report = start

    with ProcessPoolExecutor(
        max_workers=args.workers,
        initializer=_init_worker,
        initargs=(matchers,),
    ) as executor:
        futures = {executor.submit(scan_path, p): p for p in paths}
        for future in as_completed(futures):
            processed += 1
            hits, errors, bytes_read = future.result()
            total_bytes += bytes_read
            for term_str, location, line in hits:
                if location not in found_locations:
                    found_locations.add(location)
                    print(f"  ✔ Gefunden in: {location}")
            all_hits.extend(hits)
            all_errors.extend(errors)

            now = time.time()
            if now - last_report >= 5 or processed == total:
                print(f"  … {processed}/{total} Dateien durchsucht ({now - start:.0f}s vergangen)")
                last_report = now

    elapsed_total = time.time() - start

    print("\n--- Zusammenfassung ---")
    for term in terms:
        count = sum(1 for h in all_hits if h[0] == term)
        print(f"{term}: GEFUNDEN ({count} Treffer)" if count else f"{term}: NICHT GEFUNDEN")

    if all_hits:
        print("\n--- Fundstellen (zum Kopieren) ---")
        for location in dict.fromkeys(loc for _, loc, _ in all_hits):
            print(location)

    if all_errors:
        print(f"\n{len(all_errors)} Datei(en) konnten nicht gelesen werden:")
        for err in all_errors[:20]:
            print(f"  - {err}")
        if len(all_errors) > 20:
            print(f"  ... und {len(all_errors) - 20} weitere")

    gb_scanned = total_bytes / (1024 ** 3)
    throughput_mb_s = (total_bytes / (1024 ** 2)) / elapsed_total if elapsed_total > 0 else 0
    print(f"\nDurchsuchtes Volumen: {gb_scanned:.3f} GB ({throughput_mb_s:.1f} MB/s effektiv)")
    print(f"Fertig in {elapsed_total:.1f}s ({total} Dateien durchsucht).")


if __name__ == "__main__":
    main()
