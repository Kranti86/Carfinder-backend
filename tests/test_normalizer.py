"""Normalizer tests pinned to the REAL searchCarRentals response shape.

Cards live in data.content.items as {type:"CAR_CARD", content:{...}}, interleaved
with non-car entries that must be filtered. Coordinates come from
data.map.supplierLocations, joined on the pickup location name.
"""

from app.providers.rapidapi_booking import (
    _extract_currency,
    _extract_items,
    _extract_search_key,
    _normalize_card,
    build_location_index,
)

# A trimmed slice of a real response: two car cards (Corolla=large car $54,
# RAV4=SUV $61) plus the non-car entries that must be skipped.
_PAYLOAD = {
    "status": True,
    "data": {
        "search_results": [],  # now empty — must NOT be used
        "content": {
            "items": [
                {"type": "RESULTS_COUNT", "content": {"label": "2515 results"}},
                {
                    "type": "CAR_CARD",
                    "positionInList": 1,
                    "content": {
                        "badges": [
                            {"id": "mobile-only-price-constructive"},
                            {"id": "free-cancellation-constructive", "text": "Free cancellation"},
                        ],
                        "title": "Toyota Corolla",
                        "subtitle": "or similar large car",
                        "imageUrl": "https://cdn.example/corolla.png",
                        "payWhen": "Pay at pick-up",
                        "specs": "5 seats | 4 doors",
                        "vehicleSpecs": [
                            {"icon": "TRANSMISSION_AUTOMATIC", "text": "Automatic"},
                            {"icon": "MILEAGE", "text": "Unlimited mileage"},
                        ],
                        "location": {"pickup": {"location": "Jersey City - Downtown Jersey City"}},
                        "supplier": {"name": "Avis", "rating": {"score": "8.9"}},
                        "pricing": {
                            "finalPriceDisplay": "$54",
                            "originalPriceDisplay": "$60",
                            "durationText": "Price for 1 day",
                        },
                        "metadata": {"vehicleId": "785161001"},
                    },
                },
                {"type": "SHELL_REGION_VIEW", "content": {"contentType": "promo"}},
                {
                    "type": "CAR_CARD",
                    "positionInList": 3,
                    "content": {
                        "badges": [],
                        "title": "Toyota RAV4",
                        "subtitle": "or similar SUV",
                        "imageUrl": "https://cdn.example/rav4.png",
                        "payWhen": "Pay at pick-up",
                        "specs": "5 seats | 4 doors",
                        "vehicleSpecs": [{"icon": "TRANSMISSION_AUTOMATIC", "text": "Automatic"}],
                        "location": {"pickup": {"location": "New Rochelle Train Station"}},
                        "supplier": {"name": "Budget", "rating": {"score": "9.8"}},
                        "pricing": {"finalPriceDisplay": "$61", "durationText": "Price for 1 day"},
                        "metadata": {"vehicleId": "785162111"},
                    },
                },
            ]
        },
        "map": {
            "supplierLocations": [
                {
                    "carouselContent": {"body": "129 Montgomery St, Jersey City, US, 07302"},
                    "supplierPinContent": {"latLong": {"latitude": 40.716818, "longitude": -74.04015}},
                    "metaData": {"locationName": "Jersey City - Downtown Jersey City"},
                },
                {
                    "supplierPinContent": {"latLong": {"latitude": 40.911451, "longitude": -73.784272}},
                    "metaData": {"locationName": "New Rochelle Train Station"},
                },
            ]
        },
        "search_key": "SEARCHKEY123",
        "search_context": {"searchKey": "SEARCHKEY123"},
        "metadata": {"lowestVehiclePrice": {"amount": 54.0, "currency": "USD"}},
    },
}


def test_filters_to_car_cards_only():
    items = _extract_items(_PAYLOAD)
    assert len(items) == 2  # RESULTS_COUNT and SHELL_REGION_VIEW dropped
    assert all(it["type"] == "CAR_CARD" for it in items)


def test_empty_search_results_is_ignored():
    # search_results is [] — extraction must use content.items, not return empty.
    assert _extract_items(_PAYLOAD)  # non-empty


def test_extract_search_key_and_currency():
    assert _extract_search_key(_PAYLOAD) == "SEARCHKEY123"
    assert _extract_currency(_PAYLOAD) == "USD"


def test_normalizes_real_card_fields():
    idx = build_location_index(_PAYLOAD)
    card = _normalize_card(_extract_items(_PAYLOAD)[0], currency="USD", location_index=idx)
    assert card.vehicle_id == "785161001"
    assert card.name == "Toyota Corolla"
    assert card.vehicle_type == "Full-size"        # "or similar large car"
    assert card.base_price == 54.0                   # supplier amount (pay at counter)
    assert card.service_fee == 20.52                 # $54 × 0.38 platform fee
    assert card.price == 74.52                       # trip total = base + fee
    assert card.original_price == 82.8               # $60 base × 1.38
    assert card.supplier == "Avis"
    assert card.supplier_rating == 8.9               # parsed from "8.9" string
    assert card.seats == 5                           # from "5 seats | 4 doors"
    assert card.transmission == "Automatic"
    assert card.free_cancellation is True
    assert card.pay_when == "Pay at pick-up"


def test_price_is_number_not_string():
    card = _normalize_card(_extract_items(_PAYLOAD)[0], currency="USD")
    assert isinstance(card.price, float)


def test_coordinates_joined_from_map():
    idx = build_location_index(_PAYLOAD)
    card = _normalize_card(_extract_items(_PAYLOAD)[0], currency="USD", location_index=idx)
    assert card.pickup_lat == 40.716818 and card.pickup_lon == -74.04015


def test_suv_subtitle_maps_to_suv():
    idx = build_location_index(_PAYLOAD)
    rav4 = _normalize_card(_extract_items(_PAYLOAD)[1], currency="USD", location_index=idx)
    assert rav4.vehicle_type == "SUV"
    assert rav4.pickup_lat == 40.911451


def test_missing_vehicle_id_dropped():
    assert _normalize_card({"content": {"title": "Ghost"}}, currency="USD") is None


def test_unknown_shape_returns_empty():
    assert _extract_items({"data": {"content": {"items": []}}}) == []
