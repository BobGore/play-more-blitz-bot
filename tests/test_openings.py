import pytest

from openings import opening_family


@pytest.mark.parametrize(
    "raw,family",
    [
        # Chess.com slugs. Real hyphens survive, variations are folded away.
        ("Caro-Kann-Defense-Panov-Attack", "Caro-Kann Defense"),
        ("Caro-Kann-Defense", "Caro-Kann Defense"),
        ("Semi-Slav-Defense", "Semi-Slav Defense"),
        ("Slav-Defense-Modern-Three-Knights-Variation", "Slav Defense"),
        ("Indian-Game-Spielmann-Indian-Variation", "Indian Game"),
        ("Pirc-Defense-Main-Line", "Pirc Defense"),
        ("Catalan-Opening-Closed", "Catalan Opening"),
        ("Colle-System-Anti-Colle-Variation", "Colle System"),
        ("Old-Benoni-Defense", "Old Benoni Defense"),
        ("English-Opening-Neo-Catalan-Semi-Slav-Defense", "English Opening"),
        ("Reti-Opening-Sicilian-Invitation", "Reti Opening"),
        ("Kings-Indian-Attack-French-Variation", "King's Indian Attack"),
        ("Queens-Pawn-Opening-Chigorin-Variation", "Queen's Pawn Opening"),
        ("Ruy-Lopez-Exchange-Variation", "Ruy Lopez"),
        ("Scotch-Game", "Scotch Game"),
        # The London is its own family, however each site files it.
        ("London-System", "London System"),
        ("Queens-Pawn-Opening-Accelerated-London-System", "London System"),
        ("Queens-Pawn-Opening-Accelerated-London-Steinitz-Countergambit", "London System"),
        ("Queen's Pawn Game: Accelerated London System", "London System"),
        # Lichess names.
        ("Scandinavian Defense: Blackburne Gambit", "Scandinavian Defense"),
        ("Scandinavian Defense: Blackburne-Kloosterboer Gambit", "Scandinavian Defense"),
        ("Queen's Pawn Game", "Queen's Pawn Game"),
        ("Ruy Lopez: Morphy Defense", "Ruy Lopez"),
        ("Alekhine Defense: Sämisch Attack", "Alekhine Defense"),
        ("Horwitz Defense", "Horwitz Defense"),
        ("Zukertort Opening: Kingside Fianchetto", "Zukertort Opening"),
    ],
)
def test_opening_family(raw, family):
    assert opening_family(raw) == family


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_missing_opening_is_unknown(raw):
    assert opening_family(raw) == "Unknown"


def test_the_same_opening_from_both_sites_lands_in_one_family():
    assert opening_family("Caro-Kann-Defense-Exchange-Variation") == opening_family("Caro-Kann Defense: Exchange Variation")
    assert opening_family("Queens-Pawn-Opening") == "Queen's Pawn Opening"
