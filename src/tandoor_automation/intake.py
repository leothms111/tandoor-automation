"""Die Abläufe rund um die Kassenbons, ohne Ein- und Ausgabe.

Wie `workflow` für `plan`: jeder Schritt gibt zurück, was er getan hat oder mit `dry_run` tun
würde, und die CLI `bon` druckt es, die Oberfläche zeigt es. Nur `bon login` bleibt in der CLI,
die Anmeldung bei Lidl braucht einen Menschen am Browser.

Ein Lauf, der an einem Mangel scheitert, wirft nicht, sondern sagt im Ergebnis, woran er hängt.
Gebucht ist dann nichts.
"""

import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tandoor_automation import store
from tandoor_automation.classes import stock_foods
from tandoor_automation.lidl import LidlClient
from tandoor_automation.mapping import Counts, Decisions, Entry, merge, resolve
from tandoor_automation.receipts import Receipt, parse_kaufland, parse_lidl
from tandoor_automation.shopping import confirmed_until
from tandoor_automation.stock import additions, dangling, describe, payload, twin, undecided
from tandoor_automation.tandoor import TandoorClient

PDFTOTEXT = "pdftotext"
UNKNOWN_DATE = "ohne-datum"


def lidl_dir() -> Path:
    """Die Rohbons aus Lidl Plus, je Bon die Antwort der API."""
    return store.bons_dir() / "lidl"


def kaufland_dir() -> Path:
    """Kaufland gibt nichts her, was sich abholen ließe.

    Sein Beleg kommt als PDF aus der App und wird hier eingeworfen; abgelegt wird die Datei
    unverändert, gelesen bei jedem Lauf neu.
    """
    return store.bons_dir() / "kaufland"


@dataclass(frozen=True)
class Fetched:
    """Das Ergebnis von `fetch_lidl`: je neuem Bon eine Zeile, und wie viele schon vorlagen."""

    lines: list[str]
    known: int


def fetch_lidl(dry_run: bool) -> Fetched:
    """Holt alle Bons, die noch nicht vorliegen, und legt sie unverändert ab.

    Gespeichert wird die Antwort der API, nicht eine Auslese daraus: was ein Bon für den Bestand
    hergibt, entscheidet erst die Zuordnung, und ein Fehler dort soll keinen neuen Abruf kosten.

    Args:
        dry_run: Nur zeigen, was geholt würde.

    Returns:
        Die neuen Bons als Zeilen und die Zahl der schon vorliegenden.

    Raises:
        LidlAuthError: Wenn der Zugang fehlt oder abgewiesen wird.
    """
    known = _known_ids()
    lidl_dir().mkdir(parents=True, exist_ok=True)
    lines = []
    with LidlClient() as lidl:
        for summary in lidl.tickets():
            ticket_id = str(summary["id"])
            if ticket_id in known:
                continue
            lines.append(_describe(summary))
            if dry_run:
                continue
            raw = lidl.ticket(ticket_id)
            _target(summary).write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return Fetched(lines, len(known))


def _known_ids() -> set[str]:
    """Liest den Stand aus den abgelegten Dateien.

    Ein eigener Stand wäre eine zweite Wahrheit, die von der Ablage abweichen kann. Der
    Dateiname trägt die ID, damit genügt der Ordner selbst.
    """
    return {path.stem.split("_", 1)[1] for path in lidl_dir().glob("*_*.json")}


def _target(summary: dict[str, Any]) -> Path:
    """Baut den Ablagepfad: das Datum sortiert den Ordner, die ID erkennt den Bon wieder."""
    day = str(summary.get("date") or "")[:10] or UNKNOWN_DATE
    return lidl_dir() / f"{day}_{summary['id']}.json"


def _describe(summary: dict[str, Any]) -> str:
    """Fasst einen Bon in eine Zeile: Tag, Summe, ID."""
    day = str(summary.get("date") or "?")[:10]
    return f"{day}  {summary.get('totalAmount', '?'):>8}  {summary['id']}"


@dataclass(frozen=True)
class Filed:
    """Ein eingeworfener Kaufland-Beleg: eine Zeile zu ihm, und ob er neu war."""

    line: str
    new: bool


def file_pdf(path: Path, dry_run: bool) -> Filed:
    """Liest einen Kaufland-Beleg und legt ihn unter seiner Bonnummer ab.

    Die Datei wird kopiert, nicht verschoben: das Original bleibt dort liegen, wo die App es
    abgelegt hat. Ein Beleg, der schon vorliegt, wird nicht überschrieben.

    Args:
        path: Pfad des PDF.
        dry_run: Nur zeigen, was abgelegt würde.

    Returns:
        Was abgelegt wurde oder schon vorlag.

    Raises:
        ValueError: Wenn die Datei kein lesbarer Kaufland-Beleg ist.
    """
    receipt = parse_kaufland(pdf_text(path))
    target = kaufland_dir() / f"{receipt.receipt_id}.pdf"
    line = (
        f"{receipt.purchased_on}  {receipt.store}  {len(receipt.items)} Positionen  {target.name}"
    )
    if target.exists():
        return Filed(line, False)
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    return Filed(line, True)


def last_kaufland() -> date | None:
    """Das Kaufdatum des zuletzt eingeworfenen Kaufland-Belegs, ohne Beleg `None`.

    Zuletzt eingeworfen heißt: zuletzt abgelegt, nicht zuletzt gekauft. Das Datum steht vorn in
    der Bonnummer und damit im Dateinamen, das PDF muss dafür nicht gelesen werden.
    """
    filed = list(kaufland_dir().glob("*.pdf"))
    if not filed:
        return None
    newest = max(filed, key=lambda path: path.stat().st_mtime)
    return datetime.strptime(newest.stem[:8], "%Y%m%d").date()


def pdf_text(path: Path) -> str:
    """Holt die Textebene eines PDF, die Spalten so wie gedruckt.

    Gelesen wird mit `pdftotext` aus poppler-utils. Ein Fremdpaket dafür wäre eine Abhängigkeit
    mehr für einen Aufruf, den das System schon kann.

    Args:
        path: Pfad des PDF.

    Returns:
        Der Text des Belegs.

    Raises:
        ValueError: Wenn die Datei fehlt oder `pdftotext` sie nicht lesen kann.
    """
    if not path.is_file():
        raise ValueError("Datei nicht gefunden")
    try:
        done = subprocess.run(
            [PDFTOTEXT, "-layout", str(path), "-"], capture_output=True, text=True, check=True
        )
    except FileNotFoundError as missing:
        raise ValueError(f"{PDFTOTEXT} fehlt, steckt in poppler-utils") from missing
    except subprocess.CalledProcessError as broken:
        raise ValueError(f"{PDFTOTEXT} bricht ab: {broken.stderr.strip()}") from broken
    return done.stdout


def receipts() -> list[Receipt]:
    """Liest alle abgelegten Bons beider Quellen, den ältesten zuerst."""
    parsed = [
        parse_lidl(json.loads(p.read_text(encoding="utf-8"))) for p in lidl_dir().glob("*.json")
    ]
    parsed += [parse_kaufland(pdf_text(p)) for p in kaufland_dir().glob("*.pdf")]
    return sorted(parsed, key=lambda receipt: receipt.purchased_on)


@dataclass(frozen=True)
class Mapped:
    """Die Zuordnung nach `refresh_mapping`, samt den Zählungen, aus denen sie entstand."""

    entries: list[Entry]
    counts: Counts
    added: int


def refresh_mapping() -> Mapped:
    """Nimmt die Bonnamen aller abgelegten Bons in die Zuordnung auf, die neuen unentschieden.

    Returns:
        Alle Zeilen mit ihren Zählungen, und wie viele Namen neu dazukamen.
    """
    counts: Counts = Counter()
    for receipt in receipts():
        counts.update((receipt.source, item.name) for item in receipt.items)
    entries = merge(store.mapping(), counts)
    return Mapped(entries, counts, store.add_mapping(entries))


@dataclass
class Intake:
    """Das Ergebnis von `book_receipts`.

    Steht etwas in `first_run`, `open_names`, `gone` oder `missing`, ist nichts gebucht worden.
    """

    first_run: bool = False
    """Es ist noch nie gebucht worden, und ohne Startdatum liefen alle Bons in den Bestand."""
    open_names: list[str] = field(default_factory=list)
    gone: list[tuple[str, int]] = field(default_factory=list)
    """Bonnamen, deren Lebensmittel es in Tandoor nicht mehr gibt."""
    missing: list[str] = field(default_factory=list)
    """Orte und Einheiten, die Tandoor nicht kennt."""
    lines: list[tuple[str, str]] = field(default_factory=list)
    """Je Zugang, was mit ihm geschah, und die Zeile dazu."""
    booked: int = 0
    skipped: int = 0
    known: int = 0

    @property
    def blocked(self) -> bool:
        """Sagt, ob ein Mangel den Lauf angehalten hat."""
        return bool(self.first_run or self.open_names or self.gone or self.missing)


def book_receipts(tandoor: TandoorClient, since: date | None, dry_run: bool) -> Intake:
    """Bucht die Zugänge aus allen Bons, die noch nicht im Bestand angekommen sind.

    Welche Bons angekommen sind, trägt die Ablage nicht: ein gebuchter Bon sieht aus wie ein
    ungebuchter, und ein zweiter Lauf würde die Mengen verdoppeln. Gemerkt werden deshalb in der
    Datenbank auch die übersprungenen, sonst holte der nächste Lauf sie nach.

    Args:
        tandoor: Der Zugang zu Tandoor.
        since: Nur Bons ab diesem Tag buchen, ältere gelten als abgehandelt.
        dry_run: Nur zeigen, was gebucht würde.

    Returns:
        Was gebucht wurde, oder woran der Lauf hängt.
    """
    known = store.booked()
    if not known and since is None:
        return Intake(first_run=True)
    fresh = [receipt for receipt in receipts() if receipt.receipt_id not in known]
    skipped = {r.receipt_id for r in fresh if since is not None and r.purchased_on < since}
    todo = [r for r in fresh if r.receipt_id not in skipped]
    keys = {(receipt.source, item.name) for receipt in todo for item in receipt.items}
    mapping = resolve(store.mapping(), keys)
    result = Intake(open_names=undecided(todo, mapping), known=len(known))
    if result.blocked:
        return result
    result.lines = _book(tandoor, todo, mapping, dry_run, result)
    if result.blocked:
        return result
    if not dry_run:
        store.remember({receipt.receipt_id for receipt in fresh})
    result.booked, result.skipped = len(todo), len(skipped)
    return result


def _book(
    tandoor: TandoorClient,
    todo: list[Receipt],
    mapping: Decisions,
    dry_run: bool,
    result: Intake,
) -> list[tuple[str, str]]:
    """Rechnet die Zugänge aus und schreibt sie nach Tandoor, Mängel landen in `result`."""
    catalogue = tandoor.foods()
    result.gone = dangling(todo, mapping, {food.id for food in catalogue})
    if result.gone:
        return []
    foods = stock_foods(catalogue, store.categories(), store.exceptions())
    found = additions(todo, mapping, foods)
    units = tandoor.units()
    locations = {place.name: place for place in tandoor.inventory_locations()}
    missing = {a.location for a in found if a.location not in locations}
    result.missing = sorted(missing | {a.unit for a in found if a.unit not in units})
    if result.missing:
        return []
    inventory = tandoor.inventory()
    intervals = store.intervals()
    lines = []
    for addition in found:
        entry_id = twin(addition, inventory, units)
        trusted_until = confirmed_until(date.today(), addition.food_id, intervals)
        if dry_run:
            lines.append(("würde buchen", describe(addition)))
        elif entry_id is not None:
            entry = next(e for e in inventory if e.id == entry_id)
            tandoor.update_inventory_entry(entry_id, entry.amount + addition.amount, trusted_until)
            lines.append(("dazu", describe(addition)))
        else:
            tandoor.create_inventory_entry(
                payload(addition, locations[addition.location], units[addition.unit], trusted_until)
            )
            lines.append(("neu", describe(addition)))
    return lines
