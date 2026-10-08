"""Test zeitlabs payment sertializers."""

from unittest.mock import MagicMock, patch

import pytest
from django.contrib.auth import get_user_model
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview

from zeitlabs_payments.models import Cart, CartItem, CatalogueItem, Invoice
from zeitlabs_payments.serializers import CartItemSerializer, CartSerializer, CourseSerializer


@pytest.mark.django_db
@patch('zeitlabs_payments.serializers.relative_url_to_absolute_url')
def test_course_serializer_course_image_exception(mock_relative_url, base_data):  # pylint: disable=unused-argument
    course = CourseOverview.objects.get(id='course-v1:org1+1+1')
    mock_request = MagicMock(scheme='https', site=MagicMock(domain='example.com'))
    mock_relative_url.side_effect = ValueError('something went wrong')

    serializer = CourseSerializer(instance=course, context={'request': mock_request})
    data = serializer.data
    assert data['course_id'] == 'course-v1:org1+1+1'
    assert data['course_name'] == 'Custom Course 1 of org1'
    assert data['course_image'] is None


@pytest.mark.django_db
@patch('zeitlabs_payments.serializers.logger.warning')
def test_cart_item_serializer_for_paid_course_type_and_invalid_ref_id(
    mock_warning, base_data
):  # pylint: disable=unused-argument
    user = get_user_model().objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-with_invlaid_ref_id')
    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    data = CartSerializer(instance=cart).data
    assert data.get('items')[0]['details']['courses'] == []
    mock_warning.assert_called_once_with(f'CourseOverview not found for id {course_item.item_ref_id}')


@pytest.mark.django_db
@patch('zeitlabs_payments.serializers.logger.warning')
def test_get_courses_raises_if_catalogue_item_type_unsupported(mock_warning):
    catalogue_item = MagicMock()
    catalogue_item.type = 'UNSUPPORTED_TYPE'
    catalogue_item.item_ref_id = 'abc123'
    cart_item = MagicMock(spec=CartItem)
    cart_item.catalogue_item = catalogue_item
    serializer = CartItemSerializer(instance=cart_item, context={})
    assert serializer.get_details(cart_item) == {}
    mock_warning.assert_called_once_with(
        "No handler implemented for item type 'UNSUPPORTED_TYPE'. Returning empty details."
    )


@pytest.mark.django_db
def test_cart_serializer_returns_serialized_items(base_data):  # pylint: disable=unused-argument
    user = get_user_model().objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    serializer = CartSerializer(instance=cart)
    data = serializer.data
    assert data['id'] == cart.id
    assert data['user'] == user.id
    assert data['status'] == Cart.Status.PENDING
    assert data['total'] == course_item.price
    assert isinstance(data['items'], list)
    assert len(data['items']) == 1
    assert data['items'][0]['sku'] == 'custom-sku-1'
    assert data['items'][0]['type'] == course_item.type
    assert data['items'][0]['currency'] == course_item.currency
    assert data['items'][0]['original_price'] == str(course_item.price)
    assert data['items'][0]['final_price'] == str(course_item.price)
    assert data['items'][0]['details']['courses'][0]['course_id'] == 'course-v1:org1+1+1'
    assert data['items'][0]['details']['courses'][0]['course_name'] == 'Custom Course 1 of org1'


@pytest.mark.django_db
def test_cart_serializer_for_include_invoice(base_data):  # pylint: disable=unused-argument
    user = get_user_model().objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PAID)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )
    invoice = Invoice.objects.create(
        cart=cart,
        gross_total=cart.total,
        total=cart.total,
        currency=course_item.currency,
        invoice_number='TEST-12345',
        status=Invoice.InvoiceStatus.PAID
    )
    # include_invoice is unset, invoice should not be returned in data
    serializer = CartSerializer(instance=cart)
    data = serializer.data
    assert data['id'] == cart.id
    assert data['status'] == Cart.Status.PAID
    assert 'invoice' not in serializer.data

    # include_invoice is set, invoice should be returned in data for paid cart
    serializer = CartSerializer(instance=cart, context={'include_invoice': True})
    data = serializer.data
    assert data['id'] == cart.id
    assert data['status'] == Cart.Status.PAID
    assert data['invoice']['invoice_number'] == invoice.invoice_number
    assert data['invoice']['currency'] == invoice.currency
    assert data['invoice']['paid_at'] == invoice.paid_at

    cart.status = Cart.Status.PENDING
    cart.save()
    invoice.delete()

    # include_invoice is set, invoice should be None for pending cart
    serializer = CartSerializer(instance=cart, context={'include_invoice': True})
    data = serializer.data
    assert data['id'] == cart.id
    assert data['status'] == Cart.Status.PENDING
    assert data['invoice'] is None


@pytest.mark.django_db
def test_cart_serializer_for_include_user_details(base_data):  # pylint: disable=unused-argument
    user = get_user_model().objects.get(id=3)
    course_item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart = Cart.objects.create(user=user, status=Cart.Status.PENDING)
    cart.items.create(
        catalogue_item=course_item,
        original_price=course_item.price,
        final_price=course_item.price
    )

    # include_user_details is unset, only user id should be returned
    serializer = CartSerializer(instance=cart)
    data = serializer.data
    assert data['id'] == cart.id
    assert isinstance(data['user'], int)
    assert data['user'] == user.id

    # include_user_details is set, user object should be returned
    serializer = CartSerializer(instance=cart, context={'include_user_details': True})
    data = serializer.data
    assert data['id'] == cart.id
    assert isinstance(data['user'], dict)
    assert data['user']['username'] == user.username
    assert data['user']['email'] == user.email
