"""Unit tests for services/address.py — normalize_address and split_address."""

from autoreplies.services.address import normalize_address, split_address

# ── normalize_address ─────────────────────────────────────────────────────────


class TestNormalizeAddress:
    def test_lowercase(self) -> None:
        assert normalize_address("353 Flatbush Ave 4R") == "353 flatbush avenue 4r"

    def test_strips_borough_state_zip_tail(self) -> None:
        result = normalize_address("353 Flatbush Ave 4R, Brooklyn, NY, 11238")
        assert "brooklyn" not in result
        assert "11238" not in result
        assert result == "353 flatbush avenue 4r"

    def test_drops_hash_from_unit(self) -> None:
        assert normalize_address("353 Flatbush Ave #4R") == "353 flatbush avenue 4r"
        assert normalize_address("96 Mcdonough St #2") == "96 mcdonough street 2"

    def test_expands_avenue(self) -> None:
        assert normalize_address("353 Flatbush Ave 4R") == "353 flatbush avenue 4r"

    def test_expands_street(self) -> None:
        assert normalize_address("96 Macdonough St 2") == "96 mcdonough street 2"

    def test_expands_boulevard(self) -> None:
        assert "boulevard" in normalize_address("100 Ocean Blvd 1A")

    def test_expands_parkway(self) -> None:
        assert "parkway" in normalize_address("50 Eastern Pkwy 3B")

    def test_expands_road(self) -> None:
        assert "road" in normalize_address("10 Kings Rd 2C")

    def test_expands_drive(self) -> None:
        assert "drive" in normalize_address("200 Atlantic Dr 1A")

    def test_expands_place(self) -> None:
        assert "place" in normalize_address("45 Clifton Pl 1A")

    def test_expands_lane(self) -> None:
        assert "lane" in normalize_address("5 Willow Ln 2B")

    def test_expands_court(self) -> None:
        assert "court" in normalize_address("8 Maple Ct 1B")

    def test_expands_terrace(self) -> None:
        assert "terrace" in normalize_address("30 Ocean Ter 4A")

    def test_mac_to_mc_canonicalization(self) -> None:
        assert normalize_address("96 Macdonough St 2") == "96 mcdonough street 2"
        # Already mc-prefixed is left alone
        assert "mcdonough" in normalize_address("96 Mcdonough St 2")

    def test_queens_hyphen_collapse(self) -> None:
        result = normalize_address("21-06 Linden St #3E, Queens, NY, 11385")
        assert result.startswith("2106")
        assert "21-06" not in result

    def test_queens_hyphen_collapse_applied_both_sides_symmetric(self) -> None:
        stored = normalize_address("21-06 Linden St 3E, Brooklyn, NY, 11385")
        parsed = normalize_address("2106 Linden St #3E")
        assert stored == parsed

    def test_strips_apostrophes(self) -> None:
        result = normalize_address("65 St Mark's Ave 2B")
        assert "'" not in result

    def test_collapses_whitespace(self) -> None:
        result = normalize_address("  353   Flatbush  Ave  4R  ")
        assert "  " not in result
        assert result == "353 flatbush avenue 4r"

    def test_no_comma_address_unchanged_tail(self) -> None:
        result = normalize_address("100 Main St 3A")
        assert result == "100 main street 3a"

    def test_stored_format_with_tail(self) -> None:
        stored = normalize_address("353 Flatbush Ave 4R, Brooklyn, NY, 11238")
        parsed = normalize_address("353 Flatbush Avenue #4R")
        assert stored == parsed


# ── split_address ─────────────────────────────────────────────────────────────


class TestSplitAddress:
    def test_canonical_flatbush(self) -> None:
        norm = normalize_address("353 Flatbush Avenue #4R")
        result = split_address(norm)
        assert result == ("353", "flatbush avenue", "4r")

    def test_canonical_mcdonough(self) -> None:
        norm = normalize_address("96 Macdonough St #2")
        result = split_address(norm)
        assert result == ("96", "mcdonough street", "2")

    def test_queens_hyphen_address(self) -> None:
        norm = normalize_address("2106 Linden St #3E")
        result = split_address(norm)
        assert result == ("2106", "linden street", "3e")

    def test_stored_queens_hyphen_address(self) -> None:
        norm = normalize_address("21-06 Linden St 3E, Brooklyn, NY, 11385")
        result = split_address(norm)
        assert result == ("2106", "linden street", "3e")

    def test_typo_street_name(self) -> None:
        norm = normalize_address("1965 Bergan Street #1B")
        result = split_address(norm)
        assert result == ("1965", "bergan street", "1b")

    def test_no_unit_returns_none(self) -> None:
        # "123 main street" — "street" gets parsed as unit, leaving
        # street="main", which makes the split technically succeed, but for an
        # address genuinely lacking a unit the unit comparison fails at match time.
        # If the address has only two tokens after house number, it should fail.
        result = split_address("123 main")
        assert result is None

    def test_no_house_number_returns_none(self) -> None:
        result = split_address("main street 2a")
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        assert split_address("") is None

    def test_only_digits_returns_none(self) -> None:
        assert split_address("12345") is None

    def test_unit_with_hyphen(self) -> None:
        # Hyphenated units like "1-a" are valid per the regex ([a-z0-9-]+)
        norm = normalize_address("100 Main St 1-A")
        result = split_address(norm)
        assert result is not None
        house, _street, unit = result
        assert house == "100"
        assert unit == "1-a"

    def test_multiword_street(self) -> None:
        norm = normalize_address("65 Saint Marks Avenue 2B")
        result = split_address(norm)
        assert result is not None
        house, street, unit = result
        assert house == "65"
        assert "saint" in street
        assert unit == "2b"

    def test_stored_vs_parsed_symmetric(self) -> None:
        stored_norm = normalize_address("353 Flatbush Ave 4R, Brooklyn, NY, 11238")
        parsed_norm = normalize_address("353 Flatbush Avenue #4R")
        assert split_address(stored_norm) == split_address(parsed_norm)
