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
