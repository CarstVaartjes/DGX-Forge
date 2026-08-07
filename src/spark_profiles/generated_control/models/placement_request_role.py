from typing import Literal, cast

PlacementRequestRole = Literal['entrypoint', 'worker']

PLACEMENT_REQUEST_ROLE_VALUES: set[PlacementRequestRole] = { 'entrypoint', 'worker',  }

def check_placement_request_role(value: str) -> PlacementRequestRole:
    if value in PLACEMENT_REQUEST_ROLE_VALUES:
        return cast(PlacementRequestRole, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLACEMENT_REQUEST_ROLE_VALUES!r}")
