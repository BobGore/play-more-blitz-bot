"""Group the openings the two sites name into families.

Chess.com names openings as a URL slug (Caro-Kann-Defense-Panov-Attack, with
real hyphens and word separators alike) and Lichess as "Family: Variation".
Both are folded into one readable family ("Caro-Kann Defense") so a month of
games gives a handful of rows instead of one row per variation.

Pure functions: no network, no Discord.
"""

import re

UNKNOWN = "Unknown"

# The family name ends at the first of these words: "Caro-Kann Defense",
# "Indian Game", "Queen's Pawn Opening". Names without one (Ruy Lopez ...) keep
# their first two words.
FAMILY_WORDS = {"Defense", "Defence", "Opening", "Game", "System", "Attack", "Gambit"}

# Chess.com's slugs use "-" for spaces, so real hyphens have to be put back.
HYPHENATED = ("Caro Kann", "Semi Slav", "Nimzo Indian", "Bogo Indian", "Neo Catalan")

# Chess.com's slugs drop apostrophes; Lichess keeps them.
APOSTROPHES = {"Queens": "Queen's", "Kings": "King's"}


def _from_slug(slug):
    name = slug.replace("-", " ")
    for pair in HYPHENATED:
        name = name.replace(pair, pair.replace(" ", "-"))
    return " ".join(APOSTROPHES.get(word, word) for word in name.split())


def opening_family(raw):
    """The family of an opening as either site names it. "Unknown" if there isn't one."""
    if not raw or not raw.strip():
        return UNKNOWN

    name = raw.strip()
    if " " not in name and "-" in name:
        name = _from_slug(name)

    # Chess.com files the London under "Queen's Pawn Opening: Accelerated London",
    # and Lichess under "Queen's Pawn Game: Accelerated London System". It is
    # played as an opening in its own right, so it gets its own family. Checked
    # before cutting at the colon, which would lose the word.
    if re.search(r"\bLondon\b", name):
        return "London System"

    words = name.split(":")[0].split()
    for i, word in enumerate(words):
        if word in FAMILY_WORDS:
            return " ".join(words[: i + 1])
    return " ".join(words[:2])
