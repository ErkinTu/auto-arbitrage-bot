from datetime import date
from decimal import Decimal

import pytest

from app.services.exchange import _parse_nbkr_xml

NBKR_XML = """<?xml version="1.0" encoding="windows-1251" ?>
<CurrencyRates Name="Daily Exchange Rates" Date="18.07.2026">
<Currency ISOCode="USD"><Nominal>1</Nominal><Value>87,4500</Value></Currency>
<Currency ISOCode="EUR"><Nominal>1</Nominal><Value>100,0559</Value></Currency>
</CurrencyRates>""".encode("windows-1251")


def test_parse_nbkr_xml() -> None:
    rate_date, rate = _parse_nbkr_xml(NBKR_XML)
    assert rate_date == date(2026, 7, 18)
    assert rate == Decimal("87.4500")


def test_parse_nbkr_xml_without_usd_raises() -> None:
    xml = b'<CurrencyRates Date="18.07.2026"><Currency ISOCode="EUR"><Nominal>1</Nominal><Value>100,0</Value></Currency></CurrencyRates>'
    with pytest.raises(ValueError):
        _parse_nbkr_xml(xml)
