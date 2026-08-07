from typing import Literal, cast

PlanEndpointScheme = Literal['http', 'https']

PLAN_ENDPOINT_SCHEME_VALUES: set[PlanEndpointScheme] = { 'http', 'https',  }

def check_plan_endpoint_scheme(value: str) -> PlanEndpointScheme:
    if value in PLAN_ENDPOINT_SCHEME_VALUES:
        return cast(PlanEndpointScheme, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLAN_ENDPOINT_SCHEME_VALUES!r}")
