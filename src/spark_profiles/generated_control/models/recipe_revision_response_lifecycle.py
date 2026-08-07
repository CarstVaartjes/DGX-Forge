from typing import Literal, cast

RecipeRevisionResponseLifecycle = Literal['blocked', 'deprecated', 'draft', 'resolved']

RECIPE_REVISION_RESPONSE_LIFECYCLE_VALUES: set[RecipeRevisionResponseLifecycle] = { 'blocked', 'deprecated', 'draft', 'resolved',  }

def check_recipe_revision_response_lifecycle(value: str) -> RecipeRevisionResponseLifecycle:
    if value in RECIPE_REVISION_RESPONSE_LIFECYCLE_VALUES:
        return cast(RecipeRevisionResponseLifecycle, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECIPE_REVISION_RESPONSE_LIFECYCLE_VALUES!r}")
