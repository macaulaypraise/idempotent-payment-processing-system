from typing import Annotated

from pydantic import Field

# Money is an integer number of minor units (cents, kobo) everywhere: no
# floats, no Decimal round-trips. 10^15 minor units is far above any single
# real payment and well inside BIGINT.
MAX_AMOUNT = 10**15

Amount = Annotated[
    int,
    Field(strict=True, gt=0, le=MAX_AMOUNT, description="Minor units, e.g. 1050"),
]
Currency = Annotated[
    str, Field(min_length=3, max_length=3, pattern="^[A-Z]{3}$", examples=["USD"])
]
