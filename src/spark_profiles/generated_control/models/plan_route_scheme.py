from typing import Literal, cast

PlanRouteScheme = Literal['http', 'https']

PLAN_ROUTE_SCHEME_VALUES: set[PlanRouteScheme] = { 'http', 'https',  }

def check_plan_route_scheme(value: str) -> PlanRouteScheme:
    if value in PLAN_ROUTE_SCHEME_VALUES:
        return cast(PlanRouteScheme, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {PLAN_ROUTE_SCHEME_VALUES!r}")
