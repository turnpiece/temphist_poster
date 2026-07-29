"""Unit tests for resolve_topical_location.py."""

from unittest.mock import patch

import pytz

from resolve_topical_location import prompt_timezone, slugify


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


class TestPromptTimezone:
    def test_single_zone_country_enter_accepts_default(self):
        with patch("builtins.input", side_effect=[""]):
            assert prompt_timezone("FR") == "Europe/Paris"

    def test_single_zone_country_can_be_overridden(self):
        with patch("builtins.input", side_effect=["Europe/London"]):
            assert prompt_timezone("FR") == "Europe/London"

    def test_multi_zone_country_pick_by_number(self):
        with patch("builtins.input", side_effect=["0"]):
            result = prompt_timezone("US")
        assert result in pytz.country_timezones["US"]

    def test_multi_zone_country_pick_by_typing_zone_name(self):
        with patch("builtins.input", side_effect=["America/New_York"]):
            assert prompt_timezone("US") == "America/New_York"

    def test_multi_zone_country_invalid_choice_then_valid(self):
        with patch("builtins.input", side_effect=["not-a-zone", "0"]):
            result = prompt_timezone("US")
        assert result in pytz.country_timezones["US"]

    def test_unknown_country_code_falls_back_to_manual_entry(self):
        with patch("builtins.input", side_effect=["Europe/Paris"]):
            assert prompt_timezone("ZZ") == "Europe/Paris"

    def test_unknown_country_code_invalid_then_valid(self):
        with patch("builtins.input", side_effect=["nope", "Asia/Tokyo"]):
            assert prompt_timezone("ZZ") == "Asia/Tokyo"
