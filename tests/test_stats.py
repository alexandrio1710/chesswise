"""Unit tests for opening-family grouping (Stage D)."""

from stats import opening_family


class TestOpeningFamilyLichessStyle:
    """Lichess names always use "Family: Variation" — an exact split."""

    def test_splits_on_colon(self):
        assert opening_family("Sicilian Defense: Accelerated Dragon") == "Sicilian Defense"

    def test_nested_comma_variation_still_splits_on_first_colon(self):
        assert opening_family(
            "Indian Defense: Budapest Defense, Fajarowicz Variation"
        ) == "Indian Defense"

    def test_no_colon_returns_unchanged(self):
        assert opening_family("Van't Kruijs Opening") == "Van't Kruijs Opening"


class TestOpeningFamilyChesscomStyle:
    """Chess.com names have no delimiter and often trail into a literal
    move list — truncate at the first digit.
    """

    def test_truncates_at_first_digit(self):
        assert opening_family("Englund Gambit 2.dxe5") == "Englund Gambit"

    def test_short_family_names_collapse_cleanly(self):
        # These are the cases the heuristic is specifically designed to
        # get right, validated against real fetched data.
        assert opening_family("London System 3...Bf5 4.e3 e6 5.Bd3") == "London System"
        assert opening_family("Italian Game...6.Nc3 Be7 7.O O O O") == "Italian Game"
        assert opening_family("Dutch Defense") == "Dutch Defense"

    def test_strips_trailing_with_clause(self):
        assert opening_family("Modern Defense with 1 d4 2.Bf4 Bg7 3.e3") == "Modern Defense"

    def test_cross_source_names_collapse_to_the_same_family(self):
        # The real point of this function: the same opening, named
        # differently by each site, should land in the same bucket.
        chesscom_name = "Italian Game...6.Nc3 Be7 7.O O O O"
        lichess_name = "Italian Game"
        assert opening_family(chesscom_name) == opening_family(lichess_name)

    def test_no_digit_leaves_variation_words_attached(self):
        # Documented limitation: without a real ECO database, a variation
        # name with no digit in it doesn't reliably split from the family.
        assert opening_family("Sicilian Defense Nyezhmetdinov Rossolimo Attack") == \
            "Sicilian Defense Nyezhmetdinov Rossolimo Attack"


class TestOpeningFamilyEdgeCases:
    def test_empty_string_returns_unknown(self):
        assert opening_family("") == "Unknown"

    def test_none_returns_unknown(self):
        assert opening_family(None) == "Unknown"
