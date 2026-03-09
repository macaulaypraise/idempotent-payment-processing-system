"""
Load test for Idempotent Payment Processing System
Run: poetry run locust -f tests/load/locustfile.py --host http://localhost:8000

Scenario: LegitimateUser simulates real client behaviour
- Creates payments with unique idempotency keys
- Retries with the SAME key + SAME body (must return identical response)
- Fetches payment status by ID

Key metric: duplicate charge rate must be 0%
- If retry_same_key ever returns a DIFFERENT payment_id -> idempotency is broken
- If retry_same_key returns 422 -> body mismatch or validation error
"""
