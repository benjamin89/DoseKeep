from datetime import date

from app.gs1 import parse_medicine_code


def test_parses_a_french_medicine_data_matrix():
    result = parse_medicine_code("(01)03400930330531(21)31PF39M8GW(17)280930(10)8219107")

    assert result == {
        "raw": "(01)03400930330531(21)31PF39M8GW(17)280930(10)8219107",
        "gtin": "03400930330531",
        "serial_number": "31PF39M8GW",
        "expiry_date": date(2028, 9, 30),
        "batch_number": "8219107",
    }

