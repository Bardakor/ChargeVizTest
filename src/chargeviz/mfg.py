from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from chargeviz.models import OCPI_STATUSES, EVSEObservation, ParsedSnapshot


class PayloadError(ValueError):
    """The remote response is not a complete usable snapshot."""


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"{field} must be a non-empty string")
    return value.strip()


def _utc_timestamp(value: object, field: str) -> str:
    raw = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise PayloadError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise PayloadError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _locations(document: object) -> list[object]:
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        raise PayloadError("response must contain a list of locations")

    candidate: object = document.get("data")
    if isinstance(candidate, dict):
        candidate = candidate.get("locations")
    if not isinstance(candidate, list):
        candidate = document.get("locations")
    if not isinstance(candidate, list):
        raise PayloadError("response must contain a list of locations")
    return candidate


class MFGAdapter:
    source = "mfg"

    def parse(self, payload: bytes) -> ParsedSnapshot:
        try:
            document: Any = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PayloadError("response must be valid JSON") from error

        locations = _locations(document)
        if not locations:
            raise PayloadError("snapshot must contain at least one location")

        normalized: dict[tuple[str, str], EVSEObservation] = {}
        connector_count = 0
        duplicate_count = 0
        unknown_status_count = 0
        unrecognized_status_count = 0

        for location_index, location in enumerate(locations):
            if not isinstance(location, dict):
                raise PayloadError(f"location[{location_index}] must be an object")
            location_id = _required_text(location.get("id"), "location.id")
            evses = location.get("evses", [])
            if not isinstance(evses, list):
                raise PayloadError(f"location {location_id!r} evses must be a list")

            for evse_index, evse in enumerate(evses):
                if not isinstance(evse, dict):
                    raise PayloadError(
                        f"location {location_id!r} evse[{evse_index}] must be an object"
                    )
                uid = _required_text(evse.get("uid"), "evse.uid")
                status = _required_text(evse.get("status"), "evse.status").upper()
                last_updated = _utc_timestamp(evse.get("last_updated"), "evse.last_updated")
                evse_id_value = evse.get("evse_id")
                evse_id = (
                    _required_text(evse_id_value, "evse.evse_id")
                    if evse_id_value is not None
                    else None
                )
                connectors = evse.get("connectors", [])
                if not isinstance(connectors, list):
                    raise PayloadError(f"EVSE {uid!r} connectors must be a list")
                connector_count += len(connectors)

                observation = EVSEObservation(
                    source=self.source,
                    location_id=location_id,
                    evse_uid=uid,
                    evse_id=evse_id,
                    status=status,
                    source_last_updated=last_updated,
                )
                key = (location_id, uid)
                previous = normalized.get(key)
                if previous is not None:
                    if previous != observation:
                        raise PayloadError(
                            f"conflicting duplicate EVSE {uid!r} at location {location_id!r}"
                        )
                    duplicate_count += 1
                    continue
                normalized[key] = observation
                unknown_status_count += int(status == "UNKNOWN")
                unrecognized_status_count += int(status not in OCPI_STATUSES)

        if not normalized:
            raise PayloadError("snapshot must contain at least one EVSE")

        observations = tuple(
            normalized[key] for key in sorted(normalized, key=lambda item: (item[0], item[1]))
        )
        return ParsedSnapshot(
            observations=observations,
            location_count=len(locations),
            connector_count=connector_count,
            duplicate_count=duplicate_count,
            unknown_status_count=unknown_status_count,
            unrecognized_status_count=unrecognized_status_count,
        )
