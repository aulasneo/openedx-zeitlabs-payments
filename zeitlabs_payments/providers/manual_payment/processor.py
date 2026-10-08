"""Base processor."""
import logging
from typing import Any

from django.db import transaction

from zeitlabs_payments.exceptions import InvalidCartError
from zeitlabs_payments.helpers import get_currency
from zeitlabs_payments.models import Cart
from zeitlabs_payments.providers.base import BaseProcessor

logger = logging.getLogger(__name__)


class ManualPaymentProcessor(BaseProcessor):
    """Manual payment processor."""

    SLUG = 'manual'
    CHECKOUT_TEXT = ''
    NAME = 'Manual Payment'

    def get_transaction_parameters(
        self,
        cart: Cart,
        request: Any = None,
        use_client_side_checkout: bool = False,
        **kwargs: Any
    ) -> dict:
        """
        Generate transaction parameters required by the processor.

        :param cart: The cart object/dictionary
        :param request: The incoming request object
        :param use_client_side_checkout: Flag for client-side checkout
        :param kwargs: Additional parameters
        :return: A dictionary of transaction parameters
        """
        raise NotImplementedError

    def process_payment(
        self,
        request: Any,
        cart: Cart,
        transaction_id: str,
        transaction_status: str,
        reason: str = '',
    ) -> dict:
        """
        Creates a paid cart, invoice, fulfills the cart, and logs the action.
        :param user: User instance
        :param course_catalog_item: CatalogueItem instance
        :param request: DRF request object
        :return: dict with created_cart and created_invoice
        :raises Exception: if anything fails
        """
        with transaction.atomic():
            cart = Cart.objects.select_for_update().get(pk=cart.pk)
            if cart.status == Cart.Status.PAID:
                transaction_record = self.get_payment_for_recovery(cart, transaction_id)
            elif cart.status in (Cart.Status.PENDING, Cart.Status.PROCESSING, Cart.Status.PAYMENT_PENDING):
                transaction_record = self.handle_payment(
                    cart=cart,
                    user=request.user,
                    transaction_status=transaction_status,
                    transaction_id=transaction_id,
                    method=self.SLUG,
                    amount=str(cart.total),
                    currency=get_currency(cart),
                    reason=reason,
                    response=None,
                    record_webhook_event=False,
                )
            else:
                raise InvalidCartError('Cannot process payment for a cancelled or refunded cart.')
        # Leave the payment transaction before starting independently committed
        # fulfillment stages. A later failure must preserve the recorded payment.
        if transaction_record is None:
            raise InvalidCartError('No unique recorded payment matches this recovery request.')
        invoice = self.complete_paid_cart(cart, request, transaction_record)
        logger.info(f'Successfully fulfilled cart {cart.id} and created invoice {invoice.id}.')
        return {
            'created_cart': cart.id,
            'created_invoice': invoice.invoice_number
        }
