import uuid
from datetime import datetime

from pydantic import BaseModel, model_validator

from app.schemas.money import Amount, Currency


class WalletRequest(BaseModel):
    currency: Currency


class WalletResponse(BaseModel):
    wallet_id: uuid.UUID
    currency: str
    available: int
    pending: int
    created_at: datetime


class BalanceResponse(BaseModel):
    wallet_id: uuid.UUID
    currency: str
    available: int
    pending: int


class EntryResponse(BaseModel):
    entry_id: int
    transfer_id: uuid.UUID
    account: str  # "available" or "pending"
    direction: str
    amount: int
    currency: str
    created_at: datetime


class EntriesPage(BaseModel):
    wallet_id: uuid.UUID
    entries: list[EntryResponse]
    next_before: int | None  # pass as ?before= to fetch the next (older) page


class TransferRequest(BaseModel):
    from_wallet_id: uuid.UUID
    to_wallet_id: uuid.UUID
    amount: Amount

    @model_validator(mode="after")
    def _distinct_wallets(self) -> "TransferRequest":
        if self.from_wallet_id == self.to_wallet_id:
            raise ValueError("from_wallet_id and to_wallet_id must differ")
        return self


class TransferResponse(BaseModel):
    transfer_id: uuid.UUID
    from_wallet_id: uuid.UUID
    to_wallet_id: uuid.UUID
    amount: int
    currency: str
    created_at: datetime
