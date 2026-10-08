How-tos
#######

Recover a paid purchase
=======================

``Cart.status == PAID`` means the payment has been recorded. It does not mean
that invoice creation and enrollment have completed. ``Cart.fulfilled_at`` is
set only after all items have been fulfilled, and each ``CartItem.fulfilled_at``
records an individually completed item.

After a temporary invoice or enrollment failure, call the processor's
``process_payment_and_update_records()`` again with the same cart, processor,
and gateway transaction ID. It loads the committed cart state, reuses the
recorded payment and invoice, and resumes unfinished items. A repeated call
after completion returns the existing invoice without enrolling again.
Calls for cancelled or refunded carts, or with a payment that does not belong
to the cart and processor, do not resume fulfillment.

For an operator-triggered retry or a background job, the processor also exposes
``complete_paid_cart(cart, request, transaction_record)``. Load the existing
payment transaction from the database and supply the correct site context in
``request``. This method raises exceptions on failure; callers should report
or retry them. It never contacts the gateway or records another payment.
No automatic retry scheduler or new admin action is installed by this change.

Run recovery outside an enclosing database transaction. The recorded payment,
invoice, and each completed item commit separately, so a later failure cannot
undo earlier stages. Manual payments use the same commit boundaries. Retry
manual API requests with the same transaction ID, learner, course, and mode;
the API reuses the original cart, including when enrollment already succeeded.
Changing the learner or course for a recorded payment is rejected.

Custom item handlers must make external side effects idempotent, for example
by using the cart item ID as an idempotency key. A local database transaction
cannot undo an external service request if checkpointing subsequently fails.
The built-in course handler's database enrollment and item checkpoint share
the same transaction.

Apply the new migrations before restarting the application. The migration
marks historical purchases as fulfilled only when a ``cart_fulfilled`` audit
entry exists. Historical partially completed purchases without item
checkpoints should be reconciled before retrying custom handlers that have
external side effects.

Initiate a payment safely
=========================

Submit a CSRF-protected POST to the URL returned in payment-method metadata.
The built-in checkout renders a separate POST form for each payment method.
Authenticated GET requests to the initiation URL return HTTP 405. Anonymous
GET requests redirect to login. Neither can start a payment.
Custom checkout clients must submit the CSRF token with their POST request.

Only a pending cart owned by the authenticated learner can start a payment.
The endpoint atomically claims the cart as processing and commits that state
before invoking the processor. Repeated or overlapping requests return HTTP
409 without contacting the gateway. Processing, payment-pending, paid,
cancelled, and refunded carts cannot be restarted through this endpoint.

Initialization exceptions return HTTP 502 and an error page with the cart
reference. Processor error responses are recorded as initialization failures,
not successful redirects. The cart remains processing unless a callback has
already changed its status; that updated status is preserved even when
initialization fails. An external order may have been accepted even if the
response timed out or could not be rendered.
There is no automatic reset or retry. Support must reconcile the cart with the
gateway before allowing another payment, including when a process stops after
claiming the cart. Do not create a replacement purchase until the first
attempt's outcome is known.

The initiation view opts out of ATOMIC_REQUESTS so its claim survives later
exceptions. Call it outside any other enclosing database transaction. Custom
processors must return an HTTP error status for initialization failures and
must not automatically retry order-creation requests after ambiguous errors.

Identify a recorded payment
===========================

A transaction is unique by ``(gateway, gateway_account, gateway_transaction_id)``.
The same external ID may exist for different gateways or merchant accounts.
Payment and refund records share this namespace: record a refund using its
own external transaction ID, rather than the original payment ID. Uniqueness
uses the database's collation for these string fields.

``BaseProcessor.TRANSACTION_ACCOUNT`` supplies the non-secret, stable merchant
account namespace. Its default empty string preserves existing processors and
legacy records. A processor serving multiple accounts must set this attribute
on its instance before recording or recovering payments, using the same
namespace for callbacks, polling, and operator recovery. Do not use credentials
or transient session IDs. Before changing the namespace for an existing
account, backfill that account's historical ``Transaction.gateway_account``
values from verified gateway/account records so retries can still find them.
The manual payment API uses the manual processor's account namespace.

Database uniqueness handles simultaneous inserts. A duplicate raised by
``handle_payment()`` is translated to ``DuplicateTransactionError`` without
leaving its caller's database transaction unusable. A retry of the same
recorded payment on its paid cart resumes fulfillment and returns its invoice.
The same payment ID submitted for another cart is rejected as a duplicate;
that cart remains unchanged and gets no invoice, webhook, or fulfillment.
Cart row locks and item checkpoints serialize recording and fulfillment.

Before migration 0004, stop payment writers (including callbacks and polling)
and back up the database. The migration checks legacy gateway/transaction-ID
pairs before any schema change. If duplicates exist it stops and reports up
to ten conflicting groups, leaving all financial records intact. Reconcile
these records and their linked invoices/events with the gateway before
retrying; no automatic deletion or guessing of the correct payment occurs.
Keep writers stopped until the uniqueness constraint is installed. MySQL
schema changes cannot be rolled back if a later DDL operation fails; inspect
and repair migration state before retrying after such a failure.

The standard local suite tests the unique constraint's concurrent inserts on
SQLite and exercises the legacy migration in a disposable database. Full
recording/invoice/fulfillment races require InnoDB and run in the dedicated
MySQL CI job, under both read-committed and repeatable-read isolation. To run
locally against an explicitly supplied test MySQL server, configure
``PAYMENT_MYSQL_HOST``, ``PAYMENT_MYSQL_PORT``, ``PAYMENT_MYSQL_USER``, and
``PAYMENT_MYSQL_PASSWORD``, then run ``tox -e mysql-concurrency``. The test user
needs permission to create/drop disposable databases; the runner creates a
uniquely named schema and removes it afterward.

Serialize payment amounts
=========================

``BaseProcessor.get_transaction_parameters_base()`` returns ``amount`` as a
``Decimal`` in major currency units, exactly equal to ``cart.total``. Cart,
transaction, and invoice monetary values stay decimal throughout calculation.
The shared helper does not round, scale, or serialize the amount. Each adapter
must select its currency precision and serialize once, before signing and
submitting the same parameters.

Use ``zeitlabs_payments.amounts.quantize_amount(amount, decimal_places)`` to
set a provider's precision without changing the value. It rejects fractional
digits that cannot be represented, non-finite or negative amounts, and
non-Decimal input. Tax calculations round at the line-item boundary; provider
serialization must not round the resulting checkout total a second time.

For a provider accepting two decimal places in major units:

.. code-block:: python

    from zeitlabs_payments.amounts import quantize_amount

    amount = quantize_amount(params['amount'], 2)  # Decimal('99.90')
    text_amount = format(amount, 'f')             # '99.90', if the API expects text

For the planned FGV BRL integration, preserve two decimal places for the
``valor`` major-unit amount. The supplied API documentation shows a numeric
JSON field, so use a Decimal-aware JSON encoder, for example
``simplejson.dumps(payload, use_decimal=True)`` with the quantized Decimal.
Do not use ``float(amount)``, an integer major amount, or an encoder that turns
the numeric field into a quoted string. The adapter must declare its encoder
dependency and submit the encoded body with ``Content-Type: application/json``.
Parse numeric response amounts directly as Decimal (for example,
``json.loads(body, parse_float=Decimal)``), compare the confirmed BRL amount
with the submitted/cart amount, and pass major units to the recording helper.
This repository does not yet include an FGV adapter; the conversion helpers
and regression tests establish the local contract, not a live FGV API test.

For providers accepting integer minor units, select the currency exponent
explicitly (two for SAR/USD/BRL, zero or three where required by the API):

.. code-block:: python

    from decimal import Decimal
    from zeitlabs_payments.amounts import amount_to_minor_units

    params['amount'] = amount_to_minor_units(params['amount'], 2)  # integer 9990
    # Sign and submit params only after this conversion.
    confirmed_amount = Decimal(response['amount']).scaleb(-2)
    # Record confirmed_amount in major units, after validating the response.

External processor compatibility
-------------------------------

This changes the shared ``amount`` type from an integer rounded to major
units to Decimal major units. Review and upgrade installed external adapters
alongside this package. No provider can safely depend on the old whole-unit
rounding for fractional checkout totals.

The reference `PayFort processor
<https://github.com/zeit-labs/zeitlabs-payfort/blob/main/payfort/processor.py>`_
multiplies the shared amount by 100 and its signature helper interpolates
values as strings. With Decimal input, that leaves ``9990.00`` rather than
the integer wire representation ``9990``. Replace its
``base_params['amount'] * 100`` expression with
``amount_to_minor_units(base_params['amount'], 2)`` before signing or rendering
the payment form. Even whole-unit carts require this adapter update.
The manual processor already records ``str(cart.total)`` and needs no change.
Other installed adapters, including those using JSON or custom signatures,
must make their major/minor unit and numeric/string format explicit as well.
Do not deploy this shared contract change with unmodified PayFort adapters.

Verify fractional totals such as 99.90, 10.01, and 0.50 in checkout, submitted
parameters, confirmed transaction records, and invoices, including discounts
and taxes. Serialization rejects incompatible precision rather than silently
charging a different amount. Authentication and amount/currency validation
of real gateway confirmations remain the responsibility of the adapter.

Validate a payment outcome
==========================

The shared processing boundary accepts only a confirmed successful payment.
``PaymentOutcome`` defines ``success``, ``pending``, ``failed``, and ``unknown``.
``BaseProcessor.normalize_payment_status()`` matches explicit status names
case-insensitively after trimming surrounding whitespace. Non-string values
and unrecognized names map to ``unknown``; values such as ``paid`` or
``authorized`` are not implicitly treated as success.

Providers can set ``TRANSACTION_STATUS_SUCCESS``,
``TRANSACTION_STATUS_PENDING``, and ``TRANSACTION_STATUS_FAILED`` to their
external status names. Canonical names remain accepted, and new successful
records always store ``success``. Blank or non-string configured aliases are
ignored, so missing confirmation always remains unknown.
Recovery accepts existing provider-specific
success records using the same mapping, but never replaces a recorded failed
or pending result with a successful retry payload.

``handle_payment()`` and the manual processor raise
``InvalidPaymentStatusError`` for any unconfirmed outcome before changing the
cart or recording a transaction. ``process_payment_and_update_records()``
returns ``None`` and records an ``invalid_transaction`` audit instead.
It validates retry payloads too, even when the cart is already paid.
These helpers reject pending and failed outcomes rather than recording them
as payments. No transaction, webhook, paid invoice, or enrollment is created,
and the cart retains its current status. A subsequent confirmed success can
use the same external ID because rejection did not consume its identity.

The staff-only manual API applies the same success check before creating or
looking up a cart. Unconfirmed and unknown values return HTTP 400; a missing
or empty status remains a missing required parameter. A successful manual
request or retry must include ``transaction_status: "success"`` (case and
surrounding whitespace are normalized).

Status normalization does not authenticate a callback or verify payment
amounts. Provider adapters must verify their gateway's confirmation before
passing a successful status. For the planned FGV integration, verified invoice
confirmation must be explicitly mapped to ``success`` before invoking the
shared helper; an unconfirmed invoice cannot trigger fulfillment. This
repository does not yet include an FGV provider or its confirmation mapping.

For historical carts marked paid with an unsuccessful recorded transaction,
recovery refuses invoice creation and enrollment. Reconcile such records
against the gateway; changing only the retry payload cannot repair them.
