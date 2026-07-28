from __future__ import annotations

import json

import pytest

from chargeviz.mfg import MFGAdapter, PayloadError


def payload(*evses: dict[str, object]) -> bytes:
    location = {
        "country_code": "GB",
        "party_id": "MFG",
        "id": "LOC-1",
        "publish": True,
        "address": "1 Test Road",
        "city": "London",
        "country": "GBR",
        "coordinates": {"latitude": "51.500000", "longitude": "-0.120000"},
        "time_zone": "Europe/London",
        "last_updated": "2026-07-27T09:00:00Z",
        "evses": list(evses),
    }
    return json.dumps({"data": [location], "status_code": 1000}).encode()


def evse(
    uid: str = "EVSE-1",
    status: str = "AVAILABLE",
    last_updated: str = "2026-07-27T09:00:00Z",
) -> dict[str, object]:
    return {
        "uid": uid,
        "evse_id": f"GB*MFG*E{uid}",
        "status": status,
        "connectors": [
            {
                "id": "1",
                "standard": "IEC_62196_T2_COMBO",
                "format": "CABLE",
                "power_type": "DC",
                "max_voltage": 920,
                "max_amperage": 400,
                "last_updated": last_updated,
            }
        ],
        "last_updated": last_updated,
    }


def test_parse_normalizes_an_ocpi_envelope() -> None:
    result = MFGAdapter().parse(payload(evse(status="charging")))

    assert result.location_count == 1
    assert result.connector_count == 1
    assert result.unknown_status_count == 0
    assert result.unrecognized_status_count == 0
    assert len(result.observations) == 1
    assert result.observations[0].source == "mfg"
    assert result.observations[0].location_id == "LOC-1"
    assert result.observations[0].evse_uid == "EVSE-1"
    assert result.observations[0].status == "CHARGING"
    assert result.observations[0].source_last_updated == "2026-07-27T09:00:00.000000Z"


def test_identical_duplicate_evse_is_collapsed_deterministically() -> None:
    duplicate = evse()

    result = MFGAdapter().parse(payload(duplicate, duplicate))

    assert len(result.observations) == 1
    assert result.duplicate_count == 1


def test_conflicting_duplicate_evse_rejects_the_whole_snapshot() -> None:
    with pytest.raises(PayloadError, match="conflicting duplicate"):
        MFGAdapter().parse(payload(evse(status="AVAILABLE"), evse(status="CHARGING")))


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (b"not-json", "valid JSON"),
        (json.dumps({"message": "rate limited"}).encode(), "list of locations"),
        (json.dumps({"data": []}).encode(), "at least one location"),
        (payload({"uid": "EVSE-1", "status": "AVAILABLE"}), "last_updated"),
    ],
)
def test_invalid_snapshot_is_rejected(document: bytes, message: str) -> None:
    with pytest.raises(PayloadError, match=message):
        MFGAdapter().parse(document)


def test_unrecognized_nonempty_status_is_retained_and_counted() -> None:
    result = MFGAdapter().parse(payload(evse(status="VENDOR_MAINTENANCE")))

    assert result.observations[0].status == "VENDOR_MAINTENANCE"
    assert result.unknown_status_count == 0
    assert result.unrecognized_status_count == 1


def test_literal_unknown_status_is_counted_separately() -> None:
    result = MFGAdapter().parse(payload(evse(status="UNKNOWN")))

    assert result.unknown_status_count == 1
    assert result.unrecognized_status_count == 0
