# Think integration for Llink prepaid credits

Think owns Mobile Money checkout and CDF amount calculation for Llink credit purchases.

## Commercial rule

- 1 Llink credit = USD 0.01.
- Configure `LLINK_CDF_PER_USD` as the approved CDF-per-USD rate.
- Think calculates `ceil_to_5(credits × 0.01 × LLINK_CDF_PER_USD)`.
- CinetPay amount is CDF, an integer multiple of 5, minimum 100 and maximum 2,500,000.
- No Llink credit purchase changes the Think monetary wallet balance.

## Legacy subscription routes

The old Llink plan catalog and internal subscription checkout return HTTP 410. Existing subscription records and refresh logic are retained only for compatibility with already-created transactions; new Llink purchases must use the credit checkout.

## New storage

The `llink_credit_purchases` table stores the requested credit amount, wallet type, CDF rate snapshot, payment transaction and status. A unique payment transaction constraint prevents one payment from backing multiple purchases.

## Internal and public routes

- `GET /api/llink/credits/catalog`
- `POST /api/internal/llink/credits/checkout`
- Existing internal payment refresh now also reconciles `llink_credits` transactions.

Only a successful provider status fetched directly from CinetPay changes the purchase status to `paid`. Failed or pending transactions do not create credits.

## Environment

Set `LLINK_CDF_PER_USD` on Think. The existing service authentication variables `LLINK_SSO_SECRET` and `LLINK_SERVICE_TOKEN` must match Llink. Keep CinetPay in Sandbox until the merchant account is approved for production.
