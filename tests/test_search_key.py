"""The search_key must decode back into the original search params, so we can
mint a fresh key when it expires. Tested against a REAL key from a live response.
"""

from app.providers.rapidapi_booking import _decode_search_key

# A real search_key captured from a live searchCarRentals response.
_REAL_KEY = (
    "eyJkcml2ZXJzQWdlIjozMCwiZHJvcE9mZkRhdGVUaW1lIjoiMjAyNi0wOS0wMVQxMjozMDowMCIsInBp"
    "Y2tVcERhdGVUaW1lIjoiMjAyNi0wOS0wMVQxMDowMDowMCIsInBpY2tVcExvY2F0aW9uIjoiNDAuNzY4"
    "MDc0MDM1NjQ0NTMsLTczLjk4MTg5NTQ0Njc3NzM0IiwicGlja1VwTG9jYXRpb25UeXBlIjoiTEFUTE9O"
    "RyIsInJlbnRhbER1cmF0aW9uSW5EYXlzIjoxLCJzZXJ2aWNlRmVhdHVyZXMiOlsiTk9fT1BBUVVFUyIs"
    "IlNVUFJFU1NfRklYRURfUFJJQ0VfVkVISUNMRVMiLCJJTkNMVURFX1BST0RVQ1RfUkVMQVRJT05TSElQ"
    "UyIsIklOQ0xVREVfRVhUUkFTX0NPTlRBSU5JTkdfRkVFUyIsIlJFVFVSTl9FWFRSQVNfV0lUSF9QUklD"
    "SU5HX0lOX01VTFRJX0NBUl9SRVNQT05TRSIsIlJFVFVSTl9NT0NLRURfTE9ZQUxUWSJdLCJzaWduYXR1"
    "cmUiOiJXa2dLNkhxVmtsc1pQOXFaRlRyQ0NWTGxEeG4wN0xvSWx1cFh6UC9MdkpRPSJ9"
)


def test_decodes_real_key():
    decoded = _decode_search_key(_REAL_KEY)
    assert decoded is not None
    assert decoded["pickUpDateTime"] == "2026-09-01T10:00:00"
    assert decoded["dropOffDateTime"] == "2026-09-01T12:30:00"
    assert decoded["driversAge"] == 30


def test_decoded_location_parses_to_coords():
    decoded = _decode_search_key(_REAL_KEY)
    lat_s, lon_s = decoded["pickUpLocation"].split(",")
    assert abs(float(lat_s) - 40.768074) < 1e-4
    assert abs(float(lon_s) - -73.981895) < 1e-4


def test_bad_key_returns_none():
    assert _decode_search_key("not-base64!!!") is None
    assert _decode_search_key("") is None
