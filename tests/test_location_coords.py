"""Coordinate join hardening: airport-name fallback + tolerant name matching."""

from app.providers import airports
from app.providers.rapidapi_booking import _normalize_card, build_location_index


def _card(location: str) -> dict:
    return {
        "type": "CAR_CARD",
        "content": {
            "title": "Chevrolet Bolt",
            "subtitle": "or similar SUV",
            "location": {"pickup": {"location": location}},
            "supplier": {"name": "Thrifty"},
            "pricing": {"finalPriceDisplay": "$161"},
            "metadata": {"vehicleId": "850390291"},
        },
    }


def test_airport_name_fallback_gives_coords():
    # No map index at all — must fall back to the airports table for LaGuardia.
    card = _normalize_card(_card("LaGuardia Airport"), currency="USD", location_index={})
    assert card.pickup_lat is not None and card.pickup_lon is not None


def test_airports_coords_for_name():
    assert airports.coords_for_name("LaGuardia Airport") is not None
    assert airports.coords_for_name("Some Random Downtown Depot") is None


def test_normalized_map_match_tolerates_whitespace_and_case():
    payload = {
        "data": {
            "map": {
                "supplierLocations": [
                    {
                        "supplierPinContent": {"latLong": {"latitude": 40.71, "longitude": -74.04}},
                        "metaData": {"locationName": "Jersey City - Downtown"},
                    }
                ]
            }
        }
    }
    idx = build_location_index(payload)
    # Card uses a differently-cased / extra-spaced version of the same name.
    card = _normalize_card(
        _card("jersey city  -  downtown"), currency="USD", location_index=idx
    )
    assert card.pickup_lat == 40.71 and card.pickup_lon == -74.04
