# Refund Policy

Policy ID: `refund-policy`

Version: `1.0`

Status: `approved`

## Eligibility

- A delivered order reported as damaged is eligible for a refund.
- An order that has not been delivered is not eligible for a refund.
- A cancelled order is not eligible for a refund.
- An order that has already been refunded must not be refunded again.

## Approval

- A refund of $200 or less may be processed automatically.
- A refund greater than $200 requires human approval before processing.

## Execution

- Every refund must use an idempotency key.
- The refund record and order status must be updated in one database transaction.
