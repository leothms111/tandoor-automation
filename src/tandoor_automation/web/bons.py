"""Die Seite Bons: holen, zuordnen und in den Bestand buchen, wie `bon fetch`, `map` und `apply`.

Die Zuordnung ist hier keine Datei mehr, sondern eine Liste offener Bonnamen, je Name ein
Suchfeld über Tandoors Lebensmittel. Ein Klick entscheidet und landet sofort in der Datenbank.
Nur `bon login` bleibt in der CLI, die Anmeldung bei Lidl braucht den eigenen Browser.
"""

import tempfile
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from tandoor_automation import intake, store
from tandoor_automation.lidl import LidlAuthError
from tandoor_automation.mapping import problems, spanning
from tandoor_automation.models import Food
from tandoor_automation.web.common import Cached, Tandoor, templates

SUGGESTIONS = 4
"""So viele Lebensmittel schlägt die Seite je offenem Bonnamen vor, bevor gesucht wird."""
HITS = 8
ALIKE = 0.6
"""Ohne gemeinsames Wort muss ein Name mindestens so ähnlich sein, sonst ist es Rauschen."""
SHORTEST_WORD = 4
"""Kürzere Wörter im Bonnamen taugen nicht zum Vergleich, "Bio" oder "1kg" träfen alles."""

router = APIRouter()
_foods = Cached(lambda tandoor: tandoor.foods())


@router.get("/bons", response_class=HTMLResponse)
def receipts_page(request: Request) -> Response:
    """Das Gerüst der Seite. Die Zuordnung lädt htmx nach, sie liest alle Bons."""
    return templates.TemplateResponse(
        request,
        "bons.html",
        {"first_run": not store.booked(), "last_kaufland": intake.last_kaufland()},
    )


@router.post("/bons/lidl", response_class=HTMLResponse)
def fetch(request: Request) -> Response:
    """Holt die neuen Bons aus Lidl Plus."""
    try:
        fetched = intake.fetch_lidl(dry_run=False)
    except LidlAuthError as problem:
        lines = [
            f"{problem}. Zugang auf dem Server herstellen: docker compose run --rm app bon login"
        ]
        return templates.TemplateResponse(request, "bons_meldung.html", {"lines": lines})
    lines = [f"geholt: {line}" for line in fetched.lines]
    lines.append(f"{len(fetched.lines)} neue Bons, {fetched.known} lagen schon vor")
    return _fetched(request, lines)


@router.post("/bons/kaufland", response_class=HTMLResponse)
def upload(request: Request, dateien: list[UploadFile]) -> Response:
    """Legt hochgeladene Kaufland-Belege unter ihrer Bonnummer ab."""
    lines = []
    for upload in dateien:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "beleg.pdf"
            path.write_bytes(upload.file.read())
            try:
                filed = intake.file_pdf(path, dry_run=False)
            except ValueError as problem:
                lines.append(f"{upload.filename}: {problem}")
                continue
        lines.append(f"{'abgelegt' if filed.new else 'lag schon vor'}: {filed.line}")
    return _fetched(request, lines)


def _fetched(request: Request, lines: list[str]) -> Response:
    """Meldet, was geholt wurde, und lässt die Zuordnung darunter neu laden."""
    return templates.TemplateResponse(
        request, "bons_meldung.html", {"lines": lines}, headers={"HX-Trigger": "holen-fertig"}
    )


@router.get("/bons/zuordnung", response_class=HTMLResponse)
def mapping_list(request: Request, tandoor: Tandoor, neu: bool = False) -> Response:
    """Nimmt neue Bonnamen auf und zeigt die offenen, jeden mit Vorschlägen."""
    mapped = intake.refresh_mapping()
    foods = _foods.get(tandoor, fresh=neu)
    names = {food.id: food.name for food in foods}
    undecided = sorted(
        (entry for entry in mapped.entries if not entry.decided),
        key=lambda entry: (-entry.count, entry.receipt_name.casefold()),
    )
    return templates.TemplateResponse(
        request,
        "bons_zuordnung.html",
        {
            "undecided": [(entry, suggest(entry.receipt_name, foods)) for entry in undecided],
            "problems": problems(mapped.entries, names) + spanning(mapped.entries, mapped.counts),
            "total": len(mapped.entries),
            "added": mapped.added,
        },
    )


def suggest(receipt_name: str, foods: list[Food], limit: int = SUGGESTIONS) -> list[Food]:
    """Rät, welche Lebensmittel ein Bonname meinen könnte.

    Bonnamen sind gekürzt und in Großbuchstaben, "GOUDA JUNG SCHEIB." statt "Gouda". Gesucht
    wird deshalb nach einzelnen Wörtern im Namen des Lebensmittels, und erst wenn keines trifft,
    nach Ähnlichkeit des Ganzen. Entschieden wird trotzdem von Hand, die Vorschläge sparen nur
    das Tippen.
    """
    wanted = receipt_name.casefold()
    words = [word.strip(".,") for word in wanted.split() if len(word.strip(".,")) >= SHORTEST_WORD]

    def likeness(food: Food) -> float:
        return SequenceMatcher(None, wanted, food.name.casefold()).ratio()

    hits = [
        food
        for food in foods
        if any(word in food.name.casefold() or food.name.casefold() in word for word in words)
    ]
    if not hits:
        hits = [food for food in foods if likeness(food) >= ALIKE]
    ranked = sorted(hits, key=likeness, reverse=True)
    return ranked[:limit]


@router.get("/bons/lebensmittel", response_class=HTMLResponse)
def search(request: Request, tandoor: Tandoor, q: str = "") -> Response:
    """Lebensmittel, deren Name den Suchtext enthält, als Knöpfe zum Zuordnen."""
    needle = q.strip().casefold()
    foods = _foods.get(tandoor)
    hits = [food for food in foods if needle in food.name.casefold()] if needle else []
    hits.sort(key=lambda food: (not food.name.casefold().startswith(needle), food.name))
    return templates.TemplateResponse(
        request, "bons_treffer.html", {"foods": hits[:HITS], "searched": bool(needle)}
    )


@router.post("/bons/zuordnen", response_class=HTMLResponse)
def decide(
    request: Request,
    tandoor: Tandoor,
    name: Annotated[str, Form()],
    laden: Annotated[str, Form()] = "",
    food_id: Annotated[int | None, Form()] = None,
) -> Response:
    """Ordnet einen Bonnamen einem Lebensmittel zu, oder verwirft ihn ohne `food_id`.

    `laden` ist bei den meisten Zeilen leer, die Entscheidung gilt dann für jeden Laden.
    """
    store.decide((laden, name), food_id)
    food = next((food for food in _foods.get(tandoor) if food.id == food_id), None)
    return templates.TemplateResponse(
        request, "bons_entschieden.html", {"name": name, "food": food}
    )


@router.post("/bons/buchen", response_class=HTMLResponse)
def book(
    request: Request,
    tandoor: Tandoor,
    seit: Annotated[str, Form()] = "",
    vorschau: Annotated[bool, Form()] = False,
) -> Response:
    """Bucht die Zugänge aus den neuen Bons in den Bestand, wie `bon apply`.

    `seit` zählt nur beim ersten Lauf: ältere Bons gelten dann als abgehandelt.
    """
    since = date.fromisoformat(seit) if seit else None
    result = intake.book_receipts(tandoor, since, vorschau)
    return templates.TemplateResponse(
        request, "bons_buchung.html", {"result": result, "preview": vorschau}
    )
