"""Unit tests for resolve_topical_location.py."""

from resolve_topical_location import slugify


class TestSlugify:
    def test_simple_name(self):
        assert slugify("Biarritz") == "biarritz"

    def test_lowercases(self):
        assert slugify("BORDEAUX") == "bordeaux"

    def test_spaces_become_underscores(self):
        assert slugify("La Rochelle") == "la_rochelle"

    def test_punctuation_collapsed_to_single_underscore(self):
        assert slugify("St. Ives!!") == "st_ives"

    def test_leading_trailing_non_alphanumeric_stripped(self):
        assert slugify("  -Nice- ") == "nice"

    def test_empty_string_falls_back_to_unknown(self):
        assert slugify("") == "unknown"

    def test_only_punctuation_falls_back_to_unknown(self):
        assert slugify("---") == "unknown"
