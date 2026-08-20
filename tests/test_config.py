"""Tests for paimon_assistant.config: BAUD_RATES and SerialSettings (fixed API).

Contract (docs/requirements/REQ-0001-serial-raw-io.md):
- BAUD_RATES is a tuple of the 8 standard tiers, arbitrary manual input also legal.
- SerialSettings defaults: port='', baudrate=115200, data_bits=8, parity='N', stop_bits=1.
- Empty port is allowed; every other illegal serial parameter raises ValueError.
"""

import pytest

from paimon_assistant.config import BAUD_RATES, SerialSettings


class TestBAUDRates:
    def test_is_tuple(self):
        assert isinstance(BAUD_RATES, tuple)

    def test_exactly_8_standard_tiers(self):
        assert BAUD_RATES == (
            9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600,
        )

    def test_ascending_order(self):
        assert BAUD_RATES == tuple(sorted(BAUD_RATES))


class TestSerialSettingsDefaults:
    def test_default_port_empty(self):
        assert SerialSettings().port == ""

    def test_default_baudrate(self):
        assert SerialSettings().baudrate == 115200

    def test_default_data_bits(self):
        assert SerialSettings().data_bits == 8

    def test_default_parity(self):
        assert SerialSettings().parity == "N"

    def test_default_stop_bits(self):
        assert SerialSettings().stop_bits == 1


class TestSerialSettingsValid:
    def test_empty_port_explicitly_allowed(self):
        SerialSettings(port="")  # must not raise

    def test_named_port_allowed(self):
        assert SerialSettings(port="COM3").port == "COM3"

    def test_custom_baudrate_allowed(self):
        # requirements allow arbitrary manual baudrate input
        assert SerialSettings(baudrate=123456).baudrate == 123456

    def test_standard_variant_combo_allowed(self):
        s = SerialSettings(port="COM3", baudrate=9600, data_bits=7, parity="E", stop_bits=2)
        assert (s.baudrate, s.data_bits, s.parity, s.stop_bits) == (9600, 7, "E", 2)

    @pytest.mark.parametrize("kwargs", [
        {"data_bits": 5},
        {"data_bits": 6},
        {"data_bits": 7},
        {"parity": "E"},
        {"parity": "O"},
        {"stop_bits": 1.5},
    ])
    def test_valid_variants(self, kwargs):
        SerialSettings(**kwargs)  # must not raise


class TestSerialSettingsInvalid:
    @pytest.mark.parametrize("kwargs", [
        # baudrate: must be a positive int
        {"baudrate": 0},
        {"baudrate": -1},
        {"baudrate": -115200},
        {"baudrate": "115200"},
        {"baudrate": None},
        # data_bits: must be one of 5..8
        {"data_bits": 0},
        {"data_bits": 4},
        {"data_bits": 9},
        {"data_bits": "8"},
        {"data_bits": None},
        # parity: must be an uppercase serial letter
        {"parity": "X"},
        {"parity": "x"},
        {"parity": None},
        {"parity": 0},
        # stop_bits: must be one of 1 / 1.5 / 2
        {"stop_bits": 0},
        {"stop_bits": 3},
        {"stop_bits": "1"},
        {"stop_bits": None},
    ])
    def test_invalid_params_rejected(self, kwargs):
        with pytest.raises(ValueError):
            SerialSettings(**kwargs)
