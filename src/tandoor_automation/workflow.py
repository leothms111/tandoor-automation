"""Die Abläufe der Kette, ohne Ein- und Ausgabe.

Ein Ablauf mit Rückfrage zerfällt in zwei Schritte: der erste sagt, was zu fragen ist, der
zweite wendet die Antworten an. Dazwischen fragt die CLI im Terminal oder die Oberfläche über
Knöpfe. Ausgegeben wird hier nichts, jeder Schritt gibt zurück, was er getan hat oder mit
`dry_run` tun würde.

Zwischen den beiden Schritten kann Zeit vergehen, in der Oberfläche sogar Stunden. Der zweite
Schritt holt seinen Stand deshalb selbst aus Tandoor, wenn er dagegen schreibt, und verlässt
sich nicht auf den des ersten.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from tandoor_automation import store
from tandoor_automation.apply import (
    Slot,
    clearable,
    describe,
    match_existing,
    meal_type_of,
    payload,
    share_problems,
    split_groups,
    together_of,
    unfold,
    without,
)
from tandoor_automation.bundle import SPILLOVER, PlanBundle, week_days
from tandoor_automation.classes import fresh_foods, stock_foods, unclassified
from tandoor_automation.models import PlannedMeal
from tandoor_automation.proposal import Proposal, validate_proposal
from tandoor_automation.rating import RatingChange, changes, own_base_ratings
from tandoor_automation.rating import payload as rating_payload
from tandoor_automation.shopping import (
    Choice,
    Item,
    Need,
    Question,
    Review,
    buy_list,
    confirmed_until,
    needs,
    review,
)
from tandoor_automation.tandoor import TandoorClient


class Outcome(StrEnum):
    """Was mit einem Eintrag des Vorschlags geschehen ist."""

    EXISTS = "steht schon"
    PREVIEW = "würde schreiben"
    WRITTEN = "geschrieben"


@dataclass(frozen=True)
class LeadQuestion:
    """Die Frage, mit welchem Gericht der erste Esser auf einem geteilten Platz anfängt."""

    slot: Slot
    meal_name: str
    days: int
    times: int
    """Wie oft das Gericht, mit dem er anfängt, auf seinen Teller kommt."""
    dishes: list[str]


@dataclass(frozen=True)
class Applied:
    """Das Ergebnis von `apply_proposal`: die Mängel, oder was mit jedem Eintrag geschah."""

    problems: list[str]
    lines: list[tuple[Outcome, str]]


@dataclass(frozen=True)
class Cleared:
    """Das Ergebnis von `clear_week`.

    Steht etwas in `missing`, ist nichts gelöscht worden: eine ID, die nicht in der Woche liegt,
    ist fast immer vertippt, und dann soll auch der Rest nicht verschwinden.
    """

    mine: list[PlannedMeal]
    others: list[PlannedMeal]
    missing: list[int]


@dataclass(frozen=True)
class RatingSync:
    """Das Ergebnis von `sync_ratings`. Mit unbekannten Rezepten ist nichts geschrieben worden."""

    unknown: list[int]
    todo: list[RatingChange]
    names: dict[int, str]


@dataclass(frozen=True)
class ShoppingCheck:
    """Was vor dem Einkauf zu klären ist, und was die Woche je Lebensmittel braucht.

    `unclassified` sind die Zutaten der Woche ohne Klasse, nach Name sortiert. Sie kommen nicht
    auf die Liste und werden deshalb gemeldet.
    """

    review: Review
    demand: dict[int, Need]
    unclassified: list[str]


@dataclass(frozen=True)
class Booking:
    """Eine beantwortete Frage, wie sie in den Bestand geht."""

    question: Question
    choice: Choice
    trusted_until: date


@dataclass(frozen=True)
class Listed:
    """Das Ergebnis von `write_list`: was neu auf die Liste kam, und wie viel schon dort stand."""

    added: list[Item]
    already: int


def lead_questions(proposal: Proposal, bundle: PlanBundle) -> list[LeadQuestion]:
    """Sagt, für welche geteilten Plätze die Aufteilung eine Entscheidung ist.

    Gefragt wird nur bei einer ungeraden Zahl von Tagen. Nur dann kommt eines der Gerichte öfter
    auf denselben Teller als das andere. Unbeantwortet fängt der erste Esser mit dem ersten
    Gericht an.

    Args:
        proposal: Der Vorschlag des Wählers, noch nicht entfaltet.
        bundle: Das Bündel derselben Woche, für die Namen der Mahlzeiten.

    Returns:
        Je zu entscheidendem Platz eine Frage, in der Reihenfolge des Vorschlags.
    """
    names = {meal_type.id: meal_type.name for meal_type in bundle.meal_types}
    questions = []
    for slot, group in split_groups(proposal).items():
        meal_type_id, start, end = slot
        days = (end - start).days + 1
        if days % len(group) == 0:
            continue
        questions.append(
            LeadQuestion(
                slot,
                names.get(meal_type_id, str(meal_type_id)),
                days,
                -(-days // len(group)),
                [entry.recipe_name for entry in group],
            )
        )
    return questions


def apply_proposal(
    tandoor: TandoorClient,
    proposal: Proposal,
    bundle: PlanBundle,
    leads: dict[Slot, int],
    dry_run: bool,
) -> Applied:
    """Prüft einen Vorschlag gegen den aktuellen Stand in Tandoor und schreibt, was noch fehlt.

    Geschrieben wird nicht der Vorschlag selbst, sondern seine entfaltete Form: ein geteilter Platz
    zerfällt in einen Eintrag je Tag und Esser, und jeder Eintrag mit Rezept außer dem Abendessen
    trägt die Marken seiner Esser. Findet die Prüfung einen Mangel, wird gar nichts geschrieben.

    Args:
        tandoor: Der Zugang zu Tandoor.
        proposal: Der Vorschlag des Wählers, noch nicht entfaltet.
        bundle: Ein frisch gebautes Bündel derselben Woche. Geschrieben wird gegen den Plan, wie
            er jetzt ist, nicht gegen den, wie er beim Wählen war.
        leads: Die Antworten auf `lead_questions`, je Platz das Gericht in seiner Gruppe.
        dry_run: Nur zeigen, was geschrieben würde.

    Returns:
        Die Mängel, oder je entfaltetem Eintrag, was mit ihm geschah.
    """
    plan = unfold(proposal, together_of(bundle), leads)
    matches = match_existing(plan, bundle)
    rest = without(bundle, [twin for _, twin in matches if twin])
    problems = validate_proposal(plan, rest, date.today()) + share_problems(plan)
    if problems:
        return Applied(problems, [])
    lines = []
    for entry, twin in matches:
        meal_type = meal_type_of(bundle, entry)
        line = describe(entry, meal_type)
        if twin is not None:
            lines.append((Outcome.EXISTS, line))
        elif dry_run or meal_type is None:
            lines.append((Outcome.PREVIEW, line))
        else:
            tandoor.create_meal(payload(entry, meal_type))
            lines.append((Outcome.WRITTEN, line))
    return Applied([], lines)


def clear_week(tandoor: TandoorClient, week: str, ids: list[int], dry_run: bool) -> Cleared:
    """Nimmt die eigenen Planeinträge einer Woche wieder aus Tandoor heraus.

    Gelöscht wird nur, was unter dem eigenen Token angelegt wurde: die Instanz ist geteilt, und
    der Platz eines Mitessers ist keine eigene Entscheidung.

    Args:
        tandoor: Der Zugang zu Tandoor.
        week: Zielwoche als ISO-Woche.
        ids: Nur diese Einträge löschen. Leer heißt: die ganze Woche.
        dry_run: Nur zeigen, was gelöscht würde.

    Returns:
        Die eigenen Einträge, die fremden und die genannten IDs, die es in der Woche nicht gibt.
    """
    days = week_days(week)
    meals = tandoor.meal_plan(days[0], days[-1] + SPILLOVER)
    mine, others = clearable(meals, days, tandoor.current_user_id(), set(ids))
    found = {meal.id for meal in mine + others}
    missing = sorted(set(ids) - found)
    if not missing and not dry_run:
        for meal in mine:
            tandoor.delete_meal(meal.id)
    return Cleared(mine, others, missing)


def sync_ratings(tandoor: TandoorClient, ratings: dict[int, int], dry_run: bool) -> RatingSync:
    """Gleicht die Grundbewertung mit Tandoors Kochprotokoll ab.

    Eine Note auf 0 wird in Tandoor gelöscht. Geändert wird der vorhandene Eintrag, denn Tandoor
    mittelt die Noten eines Rezepts, statt die neueste zu nehmen.

    Args:
        tandoor: Der Zugang zu Tandoor.
        ratings: Sterne je Rezept-ID, die Quelle.
        dry_run: Nur zeigen, was geschrieben würde.

    Returns:
        Die unbekannten Rezepte, die nötigen Änderungen und die Namen aller Rezepte.
    """
    names = {recipe.id: recipe.name for recipe in tandoor.recipes()}
    unknown = sorted(set(ratings) - set(names))
    if unknown:
        return RatingSync(unknown, [], names)
    existing = own_base_ratings(tandoor.cook_logs(), tandoor.current_user_id())
    todo = changes(ratings, existing)
    if not dry_run:
        for change in todo:
            _write_rating(tandoor, change)
    return RatingSync([], todo, names)


def rate_recipe(tandoor: TandoorClient, recipe_id: int, stars: int) -> RatingChange | None:
    """Trägt die Note eines einzelnen Rezepts nach Tandoor, wie `sync_ratings` für alle.

    Args:
        tandoor: Der Zugang zu Tandoor.
        recipe_id: Das Rezept.
        stars: Die neue Note, 0 löscht die Grundbewertung.

    Returns:
        Was geschrieben wurde, oder nichts, wenn Tandoor die Note schon trug.
    """
    existing = own_base_ratings(tandoor.cook_logs(recipe_id), tandoor.current_user_id())
    todo = changes({recipe_id: stars}, existing)
    for change in todo:
        _write_rating(tandoor, change)
    return todo[0] if todo else None


def _write_rating(tandoor: TandoorClient, change: RatingChange) -> None:
    """Legt die Grundbewertung an, ändert oder löscht sie, je nach Vorhaben."""
    if not change.stars and change.log_id is not None:
        tandoor.delete_cook_log(change.log_id)
    elif change.log_id is not None:
        tandoor.update_cook_log(change.log_id, rating_payload(change))
    else:
        tandoor.create_cook_log(rating_payload(change))


def shopping_check(tandoor: TandoorClient, week: str, today: date) -> ShoppingCheck:
    """Stellt zusammen, was vor dem Einkauf zu fragen ist und was ungefragt auf die Liste geht.

    Der Schritt liegt zwischen Plan und Einkauf: die Gerichte müssen stehen, damit die Fragen
    entstehen, und die Bons der Vorwoche müssen gebucht sein, damit die Staffel auf dem richtigen
    Bestand rechnet.

    Args:
        tandoor: Der Zugang zu Tandoor.
        week: Zielwoche als ISO-Woche.
        today: Fällig ist, was bis zum Sonntag dieser Woche abläuft.

    Returns:
        Die Fragen samt dem Bedarf der Woche je Lebensmittel.
    """
    categories = store.categories()
    exceptions = store.exceptions()
    foods = tandoor.foods()
    demand = _planned_needs(tandoor, week)
    found = review(
        set(demand),
        tandoor.inventory(),
        stock_foods(foods, categories, exceptions),
        fresh_foods(foods, categories, exceptions),
        {unit_id: name for name, unit_id in tandoor.units().items()},
        today,
    )
    missing = unclassified(foods, categories, exceptions)
    return ShoppingCheck(
        found, demand, sorted(missing[food_id] for food_id in demand if food_id in missing)
    )


def _planned_needs(tandoor: TandoorClient, week: str) -> dict[int, Need]:
    """Sammelt, was die Gerichte einer Woche brauchen, je Lebensmittel.

    Die Zutaten stehen nur im Detailabruf, das ist ein Aufruf je Rezept. Bei einer Woche sind das
    eine Handvoll, die Rezeptliste im Ganzen zu holen wäre teurer. Jedes Rezept wird einmal
    geholt, auch wenn es zweimal im Plan steht.
    """
    days = week_days(week)
    meals = tandoor.meal_plan(days[0], days[-1] + SPILLOVER)
    details = {
        recipe_id: tandoor.recipe_detail(recipe_id)
        for recipe_id in sorted({meal.recipe_id for meal in meals if meal.recipe_id})
    }
    return needs(meals, details)


def book_answers(
    tandoor: TandoorClient,
    found: Review,
    answers: dict[int, Choice],
    today: date,
    dry_run: bool,
) -> list[Booking]:
    """Schreibt die Antworten in den Bestand.

    Unbeantwortete Fragen fassen den Bestand nicht an: keine Antwort ist keine Bestätigung. Auf
    die Liste kommen sie trotzdem, siehe `write_list`.

    Args:
        tandoor: Der Zugang zu Tandoor.
        found: Die gestellten Fragen.
        answers: Die gewählte Antwort je Eintrags-ID, nur für die beantworteten Fragen.
        today: Der Tag der Antwort.
        dry_run: Nur zeigen, was gebucht würde.

    Returns:
        Je beantworteter Frage, was gebucht wurde und bis wann es trägt.
    """
    intervals = store.intervals()
    bookings = []
    for question in found.questions:
        choice = answers.get(question.entry_id)
        if choice is None:
            continue
        trusted_until = confirmed_until(today, question.food_id, intervals)
        if not dry_run:
            tandoor.update_inventory_entry(question.entry_id, choice.amount, trusted_until)
        bookings.append(Booking(question, choice, trusted_until))
    return bookings


def write_list(
    tandoor: TandoorClient, check: ShoppingCheck, answers: dict[int, Choice], dry_run: bool
) -> Listed:
    """Schreibt das Fehlende auf die Einkaufsliste, je Gericht ein Eintrag.

    Braucht die Woche ein Lebensmittel für zwei Gerichte, stehen zwei Einträge da, jeder an
    seinem Rezeptblock. Tandoor zeigt dann eine Zeile mit 2 und beiden Gerichten, bei einem
    Gericht keine Zahl. Vorratsware ohne Gericht bekommt einen Eintrag ohne Block.

    Ein Block wird erst angelegt, wenn etwas aus seinem Gericht gekauft wird, und hängt schon
    einer am Planeintrag, wird er wiederverwendet.

    Args:
        tandoor: Der Zugang zu Tandoor.
        check: Die Fragen samt Bedarf, aus `shopping_check`.
        answers: Die gewählte Antwort je Eintrags-ID, nur für die beantworteten Fragen.
        dry_run: Nur zeigen, was auf die Liste käme.

    Returns:
        Was neu auf die Liste kam, und wie viel davon schon dort stand.
    """
    listed = tandoor.shopping_list()
    blocks = tandoor.shopping_recipes()
    wanted = buy_list(check.review, answers)
    todo = [item for item in wanted if item.food_id not in listed]
    if not dry_run:
        for item in todo:
            need = check.demand.get(item.food_id)
            for meal in list(need.recipes.values()) if need else [None]:
                block_id = None
                if meal is not None:
                    if meal.id not in blocks:
                        blocks[meal.id] = tandoor.create_shopping_recipe(meal)
                    block_id = blocks[meal.id]
                tandoor.create_shopping_entry(item.food_id, block_id)
    return Listed(todo, len(wanted) - len(todo))
