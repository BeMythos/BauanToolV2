# JUBA Searching Tool GDV-Suchtool


Ein Werkzeug, um in großen Bestandsdaten-Ordnern (inkl. **ZIP-Archiven** und deren
Inhalten) rekursiv nach **Versicherungsnummern** oder beliebigen Textbegriffen zu
suchen. Es zeigt an, **in welcher Datei** eine Nummer vorkommt – wahlweise auf der
Kommandozeile oder über eine **grafische Oberfläche**.

> **Nur-Lesend:** Das Tool öffnet alle Dateien/ZIPs ausschließlich lesend. Im
> durchsuchten Datenbestand wird nichts geschrieben, umbenannt, verschoben oder
> gelöscht. Ergebnisse erscheinen nur auf dem Bildschirm.

---

## Inhalt

| Datei | Zweck |
|-------|-------|
| `script.py` | Suchmaschine + Kommandozeilen-Tool (CLI) |
| `such_gui.py` | Grafische Oberfläche (nutzt `script.py` als Motor) |

Beide Dateien müssen im **selben Ordner** liegen. Es werden **keine Zusatzpakete**
benötigt – nur eine Standard-Python-Installation (3.9+). `tkinter` für die GUI ist
bei Windows-Python bereits enthalten.

---

## Was das Tool kann

- **Rekursive Suche** ab einem Wurzelordner über alle Unterordner hinweg.
- **ZIP-Inhalte** werden mitdurchsucht, ohne sie zu entpacken.
- **Mehrere Suchbegriffe** gleichzeitig (Nummern oder Text), optional aus einer Datei.
- **Filter**, um gezielt und schnell zu suchen:
  - **Jahr** – nur Unterordner ab einem bestimmten Jahr (`2025_04-05`, `2026_07`, …).
  - **Vertrieb** – nur bestimmte Vertriebs-Unterordner (z. B. `Prov`, `Tecis`).
  - **Konzern** – nur Dateien, deren Name einen Konzernbegriff enthält (z. B.
    `nuernberger`, `talanx`).
  - **UND-Begriffe** – eine Datei zählt nur als Treffer, wenn sie **alle**
    Pflichtbegriffe **und** die gesuchte Nummer enthält.
- **Immer-Regel:** `AenderungsBestandLieferung`- und `StichtagsBestandLieferung`-
  Dateien werden **immer** durchsucht – auch bei gesetztem Konzern-Filter, weil sie
  den Gesamtbestand über alle Konzerne enthalten können.
- **Genauigkeit:** verlustfreies Decoding (UTF-8 → cp1252 → latin-1), damit auch
  Umlaute (`Müller`, `Rückkaufswert`) in unterschiedlich kodierten Dateien zuverlässig
  gefunden werden.
- **Format-Deduplication:** Liegt eine Lieferung in mehreren Formaten vor, wird nur
  eines gelesen (Priorität `gdv.xml` > `bipro.xml` > `cleaned`); Prüfsummen-Dateien
  werden übersprungen.
- **Laptop-schonend:** Standardmäßig nur die halbe Anzahl CPU-Kerne, niedrige
  Prozess-Priorität, und ein Vorfilter, der teure Dateien nur bei möglichem Treffer
  vollständig einliest.

---

## Grafische Oberfläche (`such_gui.py`)

Für den normalen Gebrauch am komfortabelsten.

**Start:**
```bash
python such_gui.py
```
(oder Doppelklick, wenn `.py` mit Python verknüpft ist)

**Bedienung:**
1. Ordner wählen (Feld **Ordner** oder Knopf **Durchsuchen…**).
2. **Nummer(n)** eingeben (mehrere durch Leerzeichen/Komma getrennt).
3. Optional **Konzern**, **UND-Begriffe**, **Ab Jahr**, **Vertrieb** setzen.
4. **🔍 Suchen** klicken.

Jede Fundstelle erscheint sofort in der Liste. **Doppelklick auf eine Zeile öffnet
den Windows-Explorer und markiert die Datei/ZIP** – so sieht man direkt, wo sie
liegt. Rechtsklick bietet „Ordner öffnen" und „Pfad kopieren". Ein Fortschrittsbalken
und ein **Abbrechen**-Knopf sind vorhanden; die Suche läuft im Hintergrund, das
Fenster bleibt bedienbar.

> Bleibt der Rechner zu langsam: das Feld **Prozesse** auf `2` stellen.

---

## Kommandozeile (`script.py`)

**Grundform:**
```bash
python script.py [ORDNER] [NUMMER ...] [Optionen]
```

**Beispiele:**
```bash
# Standardordner und Standardnummer (im Code hinterlegt)
python script.py

# Bestimmter Ordner, eine Nummer
python script.py "D:\Bestandsdaten" 98000162790

# Mehrere Nummern
python script.py "D:\Bestandsdaten" 98000162790 440009381

# Nur Konzern Nürnberger (Änderungs-/Stichtagsbestände bleiben immer dabei)
python script.py "D:\Bestandsdaten" 98000162790 --konzern nuernberger

# Zusätzliche Pflichtbegriffe im Dateiinhalt
python script.py "D:\Bestandsdaten" 98000162790 --and Rueckkaufswert 2025

# Nur ab Jahr 2026 und nur Vertrieb Prov
python script.py "D:\Bestandsdaten" 98000162790 --ab-jahr 2026 --vertrieb prov
```

**Optionen:**

| Option | Bedeutung |
|--------|-----------|
| `--terms-file DATEI` | Textdatei mit einer Nummer pro Zeile (zusätzlich zu den angegebenen). |
| `--workers N` | Anzahl paralleler Prozesse (Default: halbe CPU-Kerne, mind. 2). |
| `--ab-jahr JAHR` | Nur Unterordner ab diesem Jahr. `Archiv` (ohne Jahres-Präfix) bleibt immer dabei. |
| `--vertrieb V …` | Nur diese Vertriebs-Unterordner (z. B. `--vertrieb prov tecis`). |
| `--konzern K …` | Nur Dateien, deren Name einen dieser Begriffe enthält. Immer-Regel bleibt aktiv. |
| `--and BEGRIFF …` | UND-Pflichtbegriffe: Datei zählt nur mit allen Begriffen. |
| `--ignore-case` | Groß-/Kleinschreibung bei Nummern und UND-Begriffen ignorieren. |

Die **Standardwerte** (Wurzelordner, Standardnummer, Jahr, Vertrieb …) stehen oben in
`script.py` als `DEFAULT_*`-Konstanten und lassen sich dort dauerhaft anpassen.

---

## Wie die Suche arbeitet (kurz)

1. **Dateien sammeln** – rekursiv ab dem Ordner; dabei greifen Jahr-, Vertrieb- und
   Konzern-Filter sowie die Format-Deduplication, **bevor** eine Datei gelesen wird.
2. **Vorfilter (verlustfrei)** – pro Datei wird zunächst auf Byte-Ebene in allen
   relevanten Kodierungen geprüft, ob ein Begriff überhaupt vorkommen kann. Nur dann
   wird die Datei vollständig zu Text dekodiert. Das spart RAM und Zeit, ohne Treffer
   zu übersehen.
3. **Zeilensuche** – im dekodierten Text wird zeilenweise exakt geprüft und die
   Fundstelle mit Datei-/ZIP-Pfad ausgegeben.

---

## Ausgabe

- **Fundstellen** als Pfad, bei ZIP-Treffern in der Form
  `…\lieferung.zip::inhalt_gdv.xml.done`.
- **Zusammenfassung** je Suchbegriff (gefunden / nicht gefunden, Trefferzahl).
- **Liste nicht lesbarer Dateien** (falls vorhanden).
- **Durchsuchtes Volumen** und Laufzeit.
