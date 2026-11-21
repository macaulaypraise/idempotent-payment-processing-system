"""Client histories: one JSON line per operation (coding companion, Stage 7.5).

Every request a client makes is recorded with its outcome:
  ok       2xx: the system acknowledged it
  fail     4xx: a deterministic rejection (stored under the key, replayable)
  unknown  5xx, timeout or connection error: it may or may not have happened

The checker rebuilds the client's view of the world (verification.model)
from this file alone, so the in-process state machine and the fault-layer
scenarios are checked by exactly the same code.
"""

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Operation:
    seq: int
    actor: str  # "client" | "worker" | "fault"
    op: str  # create_wallet | create_payment | create_refund | create_transfer | ...
    key: str | None  # the Idempotency-Key, for client writes
    request: dict[str, Any]
    status: int | None  # HTTP status; None when there was no response
    body: Any  # the response body (JSON) or an error description
    outcome: str  # ok | fail | unknown

    @property
    def acknowledged(self) -> bool:
        return self.outcome == "ok"


def outcome_for(status: int | None) -> str:
    if status is None or status >= 500:
        return "unknown"
    return "ok" if status < 400 else "fail"


@dataclass
class History:
    operations: list[Operation] = field(default_factory=list)

    def record(
        self,
        actor: str,
        op: str,
        *,
        key: str | None = None,
        request: dict[str, Any] | None = None,
        status: int | None = None,
        body: Any = None,
        outcome: str | None = None,
    ) -> Operation:
        operation = Operation(
            seq=len(self.operations),
            actor=actor,
            op=op,
            key=key,
            request=request or {},
            status=status,
            body=body,
            outcome=outcome or outcome_for(status),
        )
        self.operations.append(operation)
        return operation

    def client(self) -> Iterator[Operation]:
        return (o for o in self.operations if o.actor == "client")

    def dump(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w") as out:
            for operation in self.operations:
                out.write(json.dumps(asdict(operation), default=str) + "\n")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "History":
        with Path(path).open() as source:
            return cls.from_lines(source)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> "History":
        return cls([Operation(**json.loads(line)) for line in lines if line.strip()])
