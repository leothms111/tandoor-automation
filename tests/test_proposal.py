"""Die Prüfung, die zwischen einem Modell und dem Schreiben nach Tandoor steht.

Hier lohnen sich Tests, weil jeder Fehler still bleibt: ein vertauschtes Rezept, ein Zeitraum
neben der Woche oder ein Tag ohne Essen fällt erst auf, wenn der Plan schon in Tandoor steht und
die Einkaufsliste daraus gebaut ist.
"""

from datetime import date, datetime

import pytest

from tandoor_automation.bundle import PlanBundle
from tandoor_automation.models import MealType, PlannedMeal, Recipe
from tandoor_automation.proposal import Proposal, ProposalEntry, validate_proposal

MITTAG, ABEND = 1, 2
NAMEN = {
    1: "Lasagne vegetarisch",
    2: "Kokos-Curry",
    3: "Tomatensuppe",
    4: "Tofu-Nuggets",
    5: "Quesadillas",
    6: "Fried Rice",
    7: "Brotsalat",
}


def _bundle() -> PlanBundle:
    """Sieben Tage im September 2026, deren erster Abend schon belegt ist."""
    return PlanBundle(
        week="2026-W39",
        days=[date(2026, 9, day) for day in range(21, 28)],
        meal_types=[
            MealType(id=MITTAG, name="Mittagessen", served_at="12:00:00"),
            MealType(id=ABEND, name="Abendessen", served_at="19:00:00"),
        ],
        occupied=[
            PlannedMeal(
                id=1,
                from_date=datetime(2026, 9, 20, 19),
                to_date=datetime(2026, 9, 21, 19),
                meal_type_name="Abendessen",
                recipe_id=7,
                recipe_name=NAMEN[7],
                servings=4,
            )
        ],
        history=[],
        recipes=[
            Recipe(id=key, name=name, working_time=20, waiting_time=10, servings=4)
            for key, name in NAMEN.items()
        ],
    )


def _entry(recipe_id: int, meal_type: int, first: int, last: int, servings: float) -> ProposalEntry:
    """Ein Eintrag im September 2026, Tage als bloße Zahlen."""
    return ProposalEntry(
        recipe_id=recipe_id,
        recipe_name=NAMEN[recipe_id],
        meal_type_id=meal_type,
        from_date=date(2026, 9, first),
        to_date=date(2026, 9, last),
        servings=servings,
    )


def _proposal() -> Proposal:
    """Eine vollständige Woche nach den Regeln des Haushalts."""
    return Proposal(
        week="2026-W39",
        entries=[
            _entry(1, MITTAG, 21, 25, 5),
            _entry(2, MITTAG, 21, 25, 5),
            _entry(3, MITTAG, 26, 26, 2),
            _entry(4, MITTAG, 27, 27, 2),
            _entry(5, ABEND, 22, 23, 4),
            _entry(6, ABEND, 24, 25, 4),
            _entry(4, ABEND, 26, 26, 2),
            _entry(7, ABEND, 27, 28, 4),
        ],
    )


def test_vollstaendige_woche_ist_maengelfrei() -> None:
    assert validate_proposal(_proposal(), _bundle()) == []


def test_andere_woche_als_das_buendel() -> None:
    proposal = _proposal()
    proposal.week = "2026-W40"
    assert len(validate_proposal(proposal, _bundle())) == 1


def test_unbekanntes_rezept() -> None:
    proposal = _proposal()
    proposal.entries[0].recipe_id = 99
    assert "steht nicht im Bündel" in validate_proposal(proposal, _bundle())[0]


def test_name_passt_nicht_zur_id() -> None:
    proposal = _proposal()
    proposal.entries[0].recipe_name = "Pizza Tonno"
    assert "heißt" in validate_proposal(proposal, _bundle())[0]


def test_unbekannte_mahlzeit() -> None:
    proposal = _proposal()
    proposal.entries[0].meal_type_id = 9
    problems = validate_proposal(proposal, _bundle())
    assert any("Mahlzeit 9" in problem for problem in problems)


@pytest.mark.parametrize("first,last", [(20, 25), (28, 28)])
def test_beginn_ausserhalb_der_woche(first: int, last: int) -> None:
    proposal = _proposal()
    proposal.entries[0].from_date = date(2026, 9, first)
    proposal.entries[0].to_date = date(2026, 9, last)
    problems = validate_proposal(proposal, _bundle())
    assert any("außerhalb der Woche" in problem for problem in problems)


def test_letzter_abend_darf_einen_tag_hinausreichen() -> None:
    assert validate_proposal(_proposal(), _bundle()) == []


def test_ende_zu_weit_nach_der_woche() -> None:
    proposal = _proposal()
    proposal.entries[7].to_date = date(2026, 9, 29)
    problems = validate_proposal(proposal, _bundle())
    assert any("zu weit nach der Woche" in problem for problem in problems)


def test_ende_vor_dem_anfang() -> None:
    proposal = _proposal()
    proposal.entries[0].to_date = date(2026, 9, 20)
    problems = validate_proposal(proposal, _bundle())
    assert any("vor dem Anfang" in problem for problem in problems)


def test_null_portionen() -> None:
    proposal = _proposal()
    proposal.entries[2].servings = 0.0
    problems = validate_proposal(proposal, _bundle())
    assert any("0.0 Portionen" in problem for problem in problems)


def test_belegter_platz_wird_nicht_ueberschrieben() -> None:
    proposal = _proposal()
    proposal.entries.append(_entry(5, ABEND, 21, 21, 2))
    problems = validate_proposal(proposal, _bundle())
    assert any("trifft auf" in problem for problem in problems)


def test_belegter_platz_braucht_keine_portionen() -> None:
    proposal = _proposal()
    assert all("21 Abendessen" not in problem for problem in validate_proposal(proposal, _bundle()))


def test_zu_wenige_portionen_an_einem_tag() -> None:
    proposal = _proposal()
    proposal.entries[1].servings = 2
    problems = validate_proposal(proposal, _bundle())
    assert len(problems) == 5
    assert "2026-09-21 Mittagessen: 1.4 statt 2 Portionen" in problems


def test_fehlender_platz_faellt_auf() -> None:
    proposal = _proposal()
    del proposal.entries[3]
    problems = validate_proposal(proposal, _bundle())
    assert problems == ["2026-09-27 Mittagessen: 0.0 statt 2 Portionen"]


def test_termin_ohne_rezept_belegt_den_platz() -> None:
    proposal = _proposal()
    proposal.entries[6] = ProposalEntry(
        title="Besuch bei Anna",
        meal_type_id=ABEND,
        from_date=date(2026, 9, 26),
        to_date=date(2026, 9, 26),
    )
    assert validate_proposal(proposal, _bundle()) == []


def test_eintrag_ohne_rezept_und_ohne_titel() -> None:
    proposal = _proposal()
    proposal.entries[6] = ProposalEntry(
        meal_type_id=ABEND,
        from_date=date(2026, 9, 26),
        to_date=date(2026, 9, 26),
    )
    problems = validate_proposal(proposal, _bundle())
    assert any("weder Rezept noch Titel" in problem for problem in problems)


def test_mahlzeit_ausser_mittag_und_abend_ist_frei() -> None:
    """Ein Frühstück in Tandoor macht die Woche nicht mangelhaft, solange es niemand verlangt."""
    bundle = _bundle()
    bundle.meal_types.insert(0, MealType(id=3, name="Frühstück"))
    assert validate_proposal(_proposal(), bundle) == []
