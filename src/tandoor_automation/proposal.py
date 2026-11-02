"""Der Wochenvorschlag eines Wählers und die Prüfung, die vor dem Schreiben nach Tandoor läuft.

Der Vorschlag kommt von einem Modell und wird deshalb nicht geglaubt, sondern geprüft. Geprüft
wird nur, was sich mechanisch prüfen lässt: gibt es das Rezept, passt Name zu ID, liegt der
Zeitraum in der Woche, ist ein Platz doppelt belegt, wird jeder Platz der Woche satt. Ob die Wahl
gut ist, also abwechslungsreich, eiweißreich und vegetarisch, entscheidet das Modell.
"""

from datetime import date

from pydantic import BaseModel, Field

from tandoor_automation.bundle import SPILLOVER, PlanBundle
from tandoor_automation.models import MealType

MARKS = ("@leo", "@jazz")
"""Die Esser des Haushalts, in der Reihenfolge ihrer Wahl beim Entfalten.

`plan apply` schreibt diese Marken in die erste Zeile der `note` jedes Planeintrags. Wer die
Nährwerte eines Tages liest, findet daran seinen Anteil, ohne die Größe des Haushalts zu kennen."""
HOUSEHOLD = len(MARKS)
TOGETHER = "Abendessen"
"""Die Mahlzeit, die alle zusammen essen. Sie trägt keine Marken, SyncHealth teilt sie auf die Esser
im Haushalt auf und erkennt sie an genau diesem Namen."""
REQUIRED = ("Mittagessen", TOGETHER)
"""Die Mahlzeiten, die jeden Tag gedeckt sein müssen. Frühstück darf geplant werden, muss aber
nicht."""
TOLERANCE = 0.01


class ProposalEntry(BaseModel):
    """Ein Platz der Woche, belegt von einem Gericht oder von einem Termin.

    Der Zeitraum ist nicht der Kochtag: ein vorgekochtes Mittagessen läuft über die Arbeitswoche,
    ein Abendessen über zwei Abende. Die Uhrzeit steuert die Mahlzeit bei, `plan apply` setzt sie
    beim Schreiben ein. `recipe_name` steht neben der ID, damit die Datei lesbar bleibt und ein
    vertauschtes Rezept auffällt.

    Ein Eintrag ohne Rezept trägt stattdessen einen `title` und keine Portionen. So sieht ein Platz
    aus, an dem zwar gegessen wird, aber nicht aus der eigenen Küche: Besuch, auswärts, Urlaub.
    Tandoor kennt solche Einträge, im Plan steht dann nur der Titel.

    `marks` bleibt im Vorschlag leer: die Esser setzt `plan apply` beim Entfalten ein, nicht der
    Wähler.
    """

    recipe_id: int | None = None
    recipe_name: str = ""
    title: str = ""
    meal_type_id: int
    from_date: date
    to_date: date
    servings: float = 0
    note: str = ""
    marks: list[str] = Field(default_factory=list)


class Proposal(BaseModel):
    """Der Wochenplan, den ein Wähler aus dem Bündel zusammengestellt hat."""

    week: str
    entries: list[ProposalEntry]


def _label(entry: ProposalEntry) -> str:
    """Nennt einen Eintrag so, wie er in einer Meldung wiederzuerkennen ist."""
    return entry.recipe_name or entry.title or f"Eintrag am {entry.from_date}"


def portions_per_day(entry: ProposalEntry) -> float:
    """Verteilt die Portionen eines Eintrags gleichmäßig auf seine Tage."""
    return entry.servings / ((entry.to_date - entry.from_date).days + 1)


def _is_occupied(bundle: PlanBundle, meal_type: MealType, day: date) -> bool:
    """Prüft, ob ein Platz der Zielwoche schon in Tandoor steht."""
    return any(
        meal.meal_type_name == meal_type.name
        and meal.from_date.date() <= day <= meal.to_date.date()
        for meal in bundle.occupied
    )


def _recipe_problems(entry: ProposalEntry, bundle: PlanBundle) -> list[str]:
    """Prüft Rezept und Mahlzeit eines Eintrags gegen das Bündel."""
    problems = []
    if entry.recipe_id is None:
        if not entry.title:
            problems.append(f"{_label(entry)}: weder Rezept noch Titel")
    else:
        recipe = next(
            (candidate for candidate in bundle.recipes if candidate.id == entry.recipe_id), None
        )
        if recipe is None:
            problems.append(f"Rezept {entry.recipe_id} ('{_label(entry)}') steht nicht im Bündel")
        elif recipe.name != entry.recipe_name:
            problems.append(
                f"Rezept {entry.recipe_id} heißt '{recipe.name}', nicht '{entry.recipe_name}'"
            )
    if all(meal_type.id != entry.meal_type_id for meal_type in bundle.meal_types):
        problems.append(f"'{_label(entry)}': Mahlzeit {entry.meal_type_id} gibt es nicht")
    return problems


def _period_problems(entry: ProposalEntry, bundle: PlanBundle) -> list[str]:
    """Prüft Zeitraum und Portionen eines Eintrags.

    Der letzte Tag darf einen Tag über die Woche hinausragen: ein Abendessen kann für zwei Abende
    gekocht sein, und der zweite gehört dann schon zur nächsten Woche.
    """
    first, last = bundle.days[0], bundle.days[-1]
    problems = []
    if entry.to_date < entry.from_date:
        problems.append(f"'{_label(entry)}': endet am {entry.to_date} vor dem Anfang")
    if not first <= entry.from_date <= last:
        problems.append(f"'{_label(entry)}': beginnt am {entry.from_date} außerhalb der Woche")
    if entry.to_date > last + SPILLOVER:
        problems.append(f"'{_label(entry)}': endet am {entry.to_date}, zu weit nach der Woche")
    if entry.recipe_id is not None and entry.servings <= 0:
        problems.append(f"'{_label(entry)}': {entry.servings} Portionen")
    return problems


def _collision_problems(proposal: Proposal, bundle: PlanBundle) -> list[str]:
    """Findet Einträge, die einen in Tandoor schon belegten Platz überschreiben würden."""
    names = {meal_type.id: meal_type.name for meal_type in bundle.meal_types}
    problems = []
    for entry in proposal.entries:
        for meal in bundle.occupied:
            if meal.meal_type_name != names.get(entry.meal_type_id):
                continue
            if entry.from_date <= meal.to_date.date() and entry.to_date >= meal.from_date.date():
                belegt = meal.recipe_name or meal.title
                problems.append(
                    f"'{_label(entry)}' trifft auf '{belegt}' am {meal.from_date.date()}"
                )
    return problems


def _is_booked(proposal: Proposal, meal_type: MealType, day: date) -> bool:
    """Prüft, ob ein Platz durch einen Termin ohne Rezept belegt ist, etwa Besuch oder Urlaub."""
    return any(
        entry.recipe_id is None
        and entry.meal_type_id == meal_type.id
        and entry.from_date <= day <= entry.to_date
        for entry in proposal.entries
    )


def _coverage_problems(proposal: Proposal, bundle: PlanBundle, since: date | None) -> list[str]:
    """Findet Plätze der Woche, für die zu wenige Portionen eingeplant sind.

    Zwei Sorten Platz bleiben außen vor: einer, den Tandoor schon belegt, etwa durch das Gericht
    der Vorwoche, und einer, für den der Vorschlag selbst einen Termin ohne Rezept einträgt. An
    beiden wird gegessen, nur nicht aus der eigenen Küche. Tage vor `since` zählen auch nicht, ein
    Plan, der mitten in der Woche geändert wird, kann sie nicht mehr decken.
    """
    problems = []
    for day in bundle.days:
        if since is not None and day < since:
            continue
        for meal_type in bundle.meal_types:
            if meal_type.name not in REQUIRED:
                continue
            if _is_occupied(bundle, meal_type, day) or _is_booked(proposal, meal_type, day):
                continue
            served = sum(
                portions_per_day(entry)
                for entry in proposal.entries
                if entry.meal_type_id == meal_type.id and entry.from_date <= day <= entry.to_date
            )
            if served < HOUSEHOLD - TOLERANCE:
                problems.append(f"{day} {meal_type.name}: {served:.1f} statt {HOUSEHOLD} Portionen")
    return problems


def validate_proposal(
    proposal: Proposal, bundle: PlanBundle, since: date | None = None
) -> list[str]:
    """Prüft einen Vorschlag gegen das Bündel, aus dem er gewählt wurde.

    Args:
        proposal: Der Vorschlag des Wählers.
        bundle: Das Bündel derselben Woche.
        since: Erster Tag, der gedeckt sein muss, beim Ändern einer laufenden Woche heute. Ohne
            Angabe die ganze Woche.

    Returns:
        Alle gefundenen Mängel als Klartextzeilen. Leer heißt: der Vorschlag darf geschrieben
        werden.
    """
    if proposal.week != bundle.week:
        return [f"Vorschlag gilt {proposal.week}, das Bündel {bundle.week}"]
    problems = []
    for entry in proposal.entries:
        problems += _recipe_problems(entry, bundle)
        problems += _period_problems(entry, bundle)
    problems += _collision_problems(proposal, bundle)
    return problems + _coverage_problems(proposal, bundle, since)
