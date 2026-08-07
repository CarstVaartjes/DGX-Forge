from typing import Literal, cast

EnrollmentGrantResponsePurpose = Literal['new-node', 'rust-migration']

ENROLLMENT_GRANT_RESPONSE_PURPOSE_VALUES: set[EnrollmentGrantResponsePurpose] = { 'new-node', 'rust-migration',  }

def check_enrollment_grant_response_purpose(value: str) -> EnrollmentGrantResponsePurpose:
    if value in ENROLLMENT_GRANT_RESPONSE_PURPOSE_VALUES:
        return cast(EnrollmentGrantResponsePurpose, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {ENROLLMENT_GRANT_RESPONSE_PURPOSE_VALUES!r}")
