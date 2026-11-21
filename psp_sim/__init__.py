"""A deliberately hostile payment provider (DESIGN 4, project guide D).

It honours its own idempotency keys and keeps the ground truth of what it
really charged, which makes it the test oracle. Faults are seeded so a
failing scenario can be replayed exactly.
"""
