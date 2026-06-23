import os
import sys
import time
import zipfile
import argparse
from threading import Lock
from concurrent.futures import ThreadPoolExecutor, as_completed

# Wurzelverzeichnis: bewusst auf den obersten Ordner gesetzt, damit nicht mehr
# manuell ein (potenziell falscher) Unterordner ausgewaehlt werden muss. Die
# rekursive Suche deckt automatisch alle Unterordner (H, Prov, SLS, Tecis, ...) ab.
DEFAULT_ROOT_DIRECTORY = r"C:\Users\lstittge\Swiss Life Deutschland\Informationslogistik & BiPRO-Services - Dokumente\DQ\17_GDV\Bestandsdaten_XMLOrdner"

# Bisherige Suchstrings zur Referenz:
# "440007990", "440009381", "440011449", "440009554"
DEFAULT_SEARCH_STRING = "53383308269"

DEFAULT_WORKERS = 16

PRINT_LOCK = Lock()


def extract_matching_lines(content, search_string):
    """Gibt alle Zeilen zurueck, die den Suchstring enthalten.

    Der billige Volltext-Check vorab vermeidet die teure Python-Schleife
    ueber alle Zeilen fuer die ueberwiegende Mehrheit der Dateien, die gar
    keinen Treffer enthalten.
    """
    if search_string not in content:
        return []
    return [line for line in content.splitlines() if search_string in line]


def search_zip(zip_path, search_string, search_bytes):
    """Durchsucht alle Dateien innerhalb einer ZIP-Datei nach dem Suchstring."""
    matches = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_file:
            for file_info in zip_file.infolist():
                if file_info.filename.endswith("/"):  # Ordner auslassen
                    continue
                try:
                    with zip_file.open(file_info) as file:
                        raw = file.read()
                except Exception as e:
                    with PRINT_LOCK:
                        print(f"Eintrag konnte nicht gelesen werden! {zip_path}!{file_info.filename}: {e}")
                    continue

                # Schneller Bytes-Check vor dem (teuren) Decodieren: spart das
                # Decodieren fuer praktisch alle Dateien ohne Treffer.
                if search_bytes not in raw:
                    continue

                content = raw.decode("utf-8", errors="ignore")
                for line in extract_matching_lines(content, search_string):
                    matches.append((f"{zip_path}::{file_info.filename}", line))
    except Exception as e:
        with PRINT_LOCK:
            print(f"ZIP-Datei konnte nicht gelesen werden! {zip_path}: {e}")
    return matches


def search_plain_file(file_path, search_string, search_bytes):
    """Durchsucht eine einzelne (nicht gezippte) Datei nach dem Suchstring."""
    matches = []
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except Exception as e:
        with PRINT_LOCK:
            print(f"Datei konnte nicht gelesen werden! {file_path}: {e}")
        return matches

    if search_bytes not in raw:
        return matches

    content = raw.decode("utf-8", errors="ignore")
    for line in extract_matching_lines(content, search_string):
        matches.append((file_path, line))
    return matches


def collect_targets(root_directory):
    """Sammelt rekursiv alle Dateien unterhalb von root_directory."""
    targets = []
    for subdir, _, files in os.walk(root_directory):
        for file in files:
            file_path = os.path.join(subdir, file)
            kind = "zip" if file.lower().endswith(".zip") else "plain"
            targets.append((kind, file_path))
    return targets


def run_search(root_directory, search_string, max_workers, output_path):
    search_bytes = search_string.encode("utf-8")

    if root_directory.lower().endswith(".zip"):
        targets = [("zip", root_directory)]
    else:
        print("Sammle Dateiliste ...")
        targets = collect_targets(root_directory)

    total = len(targets)
    print(f"{total} Dateien werden durchsucht (Worker: {max_workers}) ...")
    print("Treffer in folgenden Dateien:")

    all_matches = []
    done = 0
    progress_step = max(1, total // 100)

    with open(output_path, "w", encoding="utf-8") as out_file, \
            ThreadPoolExecutor(max_workers=max_workers) as executor:

        future_to_path = {
            executor.submit(search_zip if kind == "zip" else search_plain_file, path, search_string, search_bytes): path
            for kind, path in targets
        }

        for future in as_completed(future_to_path):
            done += 1
            try:
                matches = future.result()
            except Exception as e:
                with PRINT_LOCK:
                    print(f"Fehler bei {future_to_path[future]}: {e}")
                matches = []

            for file_path, line in matches:
                result_line = f"Gefunden in {file_path}: {line}"
                with PRINT_LOCK:
                    print(result_line)
                    out_file.write(result_line + "\n")
                    out_file.flush()
                all_matches.append(file_path)

            if done % progress_step == 0 or done == total:
                with PRINT_LOCK:
                    print(f"... {done}/{total} durchsucht ({done * 100 // total}%)")

    return all_matches


def parse_args():
    parser = argparse.ArgumentParser(
        description="Sucht einen String (z.B. Versicherungsnummer) rekursiv in Dateien und ZIP-Archiven."
    )
    parser.add_argument(
        "search_string", nargs="?", default=DEFAULT_SEARCH_STRING,
        help=f"Gesuchter String (Default: {DEFAULT_SEARCH_STRING})",
    )
    parser.add_argument(
        "root_directory", nargs="?", default=DEFAULT_ROOT_DIRECTORY,
        help="Wurzelverzeichnis oder ZIP-Datei, in der gesucht werden soll",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"Anzahl paralleler Threads (Default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--output", default=None,
        help="Pfad der Ergebnisdatei (Default: suchergebnisse_<Zeitstempel>.txt im aktuellen Verzeichnis)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    start_time = time.time()
    current_time = time.strftime("%H:%M:%S")
    print(f"Start um {current_time}")
    print(f"Suche in: {args.root_directory}")
    print(f"Suchstring: {args.search_string}")

    output_path = args.output or f"suchergebnisse_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    matches = run_search(args.root_directory, args.search_string, args.workers, output_path)

    print(f"\n{len(matches)} Treffer gefunden.")
    print(f"Ergebnisse gespeichert in: {output_path}")
    print("--- %.2f Sekunden ---" % (time.time() - start_time))
    print(current_time)
    print("Ende")


if __name__ == "__main__":
    sys.exit(main())
