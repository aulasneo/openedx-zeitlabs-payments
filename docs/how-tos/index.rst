How-tos
#######

Recover a paid purchase
======================

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
undo earlier stages. The existing manual-payment flow intentionally retains
its enclosing transaction because it records an administrative payment rather
than confirming a new external charge.

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
