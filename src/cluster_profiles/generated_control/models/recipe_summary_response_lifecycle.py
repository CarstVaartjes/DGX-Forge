from typing import Literal, cast

RecipeSummaryResponseLifecycle = Literal['blocked', 'deprecated', 'draft', 'resolved']

RECIPE_SUMMARY_RESPONSE_LIFECYCLE_VALUES: set[RecipeSummaryResponseLifecycle] = { 'blocked', 'deprecated', 'draft', 'resolved',  }

def check_recipe_summary_response_lifecycle(value: str) -> RecipeSummaryResponseLifecycle:
    if value in RECIPE_SUMMARY_RESPONSE_LIFECYCLE_VALUES:
        return cast(RecipeSummaryResponseLifecycle, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_SUMMARY_RESPONSE_LIFECYCLE_VALUES!r}")
