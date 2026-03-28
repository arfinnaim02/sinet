"""URL definitions for the restaurant application."""

from django.urls import path, include
from . import views
from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin


app_name = "restaurant"

urlpatterns = [
    # Public pages
    path("", views.home, name="home"),
    path("menu/", views.menu, name="menu"),
    path("menu/item/<int:pk>/", views.menu_item_detail, name="menu_item_detail"),
    path("about/", views.about, name="about"),
    path("contact/", views.contact, name="contact"),
    path("reviews/", views.reviews_page, name="reviews"),

    # reservation (KEEP only if you still use it)
    path("book/", views.reservation, name="reservation"),

    # admin urls (same as yours)
    path("admin/login/", views.admin_login, name="admin_login"),
    path("admin/logout/", views.admin_logout, name="admin_logout"),
    path("admin/dashboard/", views.dashboard, name="dashboard"),

    # Menu item management
    path("admin/menu/", views.menu_items_list, name="menu_items_list"),
    path("admin/menu/add/", views.add_menu_item, name="add_menu_item"),
    path("admin/menu/<int:pk>/edit/", views.edit_menu_item, name="edit_menu_item"),
    path("admin/menu/<int:pk>/delete/", views.delete_menu_item, name="delete_menu_item"),

        # Addon groups
    path("admin/addon-groups/", views.addon_groups_list, name="addon_groups_list"),
    path("admin/addon-groups/add/", views.addon_group_add, name="addon_group_add"),
    path("admin/addon-groups/<int:pk>/edit/", views.addon_group_edit, name="addon_group_edit"),
    path("admin/addon-groups/<int:pk>/delete/", views.addon_group_delete, name="addon_group_delete"),

    # Addon options
    path("admin/addon-options/", views.addon_options_list, name="addon_options_list"),
    path("admin/addon-options/add/", views.addon_option_add, name="addon_option_add"),
    path("admin/addon-options/<int:pk>/edit/", views.addon_option_edit, name="addon_option_edit"),
    path("admin/addon-options/<int:pk>/delete/", views.addon_option_delete, name="addon_option_delete"),

    # Menu item <-> addon group assignments
    path("admin/menu-addon-links/", views.menu_item_addon_links_list, name="menu_item_addon_links_list"),
    path("admin/menu-addon-links/add/", views.menu_item_addon_link_add, name="menu_item_addon_link_add"),
    path("admin/menu-addon-links/<int:pk>/edit/", views.menu_item_addon_link_edit, name="menu_item_addon_link_edit"),
    path("admin/menu-addon-links/<int:pk>/delete/", views.menu_item_addon_link_delete, name="menu_item_addon_link_delete"),

    # Category management
    path("admin/categories/", views.categories_list, name="categories_list"),
    path("admin/category/add/", views.add_category, name="add_category"),
    path("admin/category/<int:pk>/edit/", views.edit_category, name="edit_category"),
    path("admin/category/<int:pk>/delete/", views.delete_category, name="delete_category"),

    # Reservations management
    path("admin/reservations/", views.reservations_list, name="reservations_list"),
    path("admin/reservations/<int:pk>/", views.reservation_detail_admin, name="reservation_detail_admin"),
    path("admin/reservations/<int:pk>/status/", views.reservation_update_status, name="reservation_update_status"),

    # Promotions placeholders
    path("admin/promotions/", views.promotions_list, name="promotions_list"),
    path("admin/promotions/add/", views.add_promotion, name="add_promotion"),
    path("admin/promotions/<int:pk>/edit/", views.edit_promotion, name="edit_promotion"),
    path("admin/promotions/<int:pk>/delete/", views.delete_promotion, name="delete_promotion"),

    path("admin/delivery-coupons/", views.delivery_coupons_list, name="delivery_coupons_list"),
    path("admin/delivery-coupons/add/", views.delivery_coupon_add, name="delivery_coupon_add"),
    path("admin/delivery-coupons/<int:pk>/edit/", views.delivery_coupon_edit, name="delivery_coupon_edit"),
    path("admin/delivery-coupons/<int:pk>/delete/", views.delivery_coupon_delete, name="delivery_coupon_delete"),

    # Hero banners
    path("admin/hero-banners/", views.hero_banners_list, name="hero_banners_list"),
    path("admin/hero-banners/add/", views.hero_banner_add, name="hero_banner_add"),
    path("admin/hero-banners/<int:pk>/edit/", views.hero_banner_edit, name="hero_banner_edit"),
    path("admin/hero-banners/<int:pk>/delete/", views.hero_banner_delete, name="hero_banner_delete"),


    # Delivery
    path("delivery/location/", views.delivery_location, name="delivery_location"),
    path("delivery/calc/", views.delivery_calc, name="delivery_calc"),
    path("delivery/set-location/", views.delivery_set_location, name="delivery_set_location"),
    path("delivery/checkout/", views.delivery_checkout, name="delivery_checkout"),
    path("delivery/place-order/", views.delivery_place_order, name="delivery_place_order"),
    path("delivery/location/partial/", views.delivery_location_partial, name="delivery_location_partial"),

    # Delivery: coupon apply/remove (NEW)
    path("delivery/coupon/apply/", views.delivery_apply_coupon, name="delivery_apply_coupon"),
    path("delivery/coupon/remove/", views.delivery_remove_coupon, name="delivery_remove_coupon"),

    # Nominatim
    path("delivery/nominatim/search/", views.nominatim_search, name="nominatim_search"),
    path("delivery/nominatim/reverse/", views.nominatim_reverse, name="nominatim_reverse"),

    # Cart endpoints
    path("delivery/cart/add/", views.delivery_cart_add, name="delivery_cart_add"),
    path("delivery/cart/update/", views.delivery_cart_update, name="delivery_cart_update"),
    path("delivery/cart/summary/", views.delivery_cart_summary, name="delivery_cart_summary"),

    # Admin: delivery orders
    path("admin/delivery-orders/", views.delivery_orders_list, name="delivery_orders_list"),
    path("admin/delivery-orders/<int:pk>/", views.delivery_order_detail_admin, name="delivery_order_detail_admin"),
    path("admin/delivery-orders/<int:pk>/status/", views.delivery_order_update_status, name="delivery_order_update_status"),

    path("delivery/remove-coupon/", views.delivery_remove_coupon, name="delivery_remove_coupon"),

    path("admin/delivery-orders/bulk-update/", views.delivery_orders_bulk_update, name="delivery_orders_bulk_update"),
    path("admin/delivery-orders/bulk-delete/", views.delivery_orders_bulk_delete, name="delivery_orders_bulk_delete"),

    path("admin/reservations/bulk-update/", views.reservations_bulk_update, name="reservations_bulk_update"),
    path("admin/reservations/bulk-delete/", views.reservations_bulk_delete, name="reservations_bulk_delete"),

    path("admin/menu/bulk-update/", views.menu_items_bulk_update, name="menu_items_bulk_update"),
    path("admin/menu/bulk-delete/", views.menu_items_bulk_delete, name="menu_items_bulk_delete"),

    path("admin/loyalty/", views.loyalty_settings, name="loyalty_settings"),
    path("admin/delivery-pricing/", views.delivery_pricing_settings, name="delivery_pricing_settings"),
    path("telegram/webhook/", views.telegram_webhook, name="telegram_webhook"),

    path(
    "account/orders/<int:order_id>/received/",
    views.customer_mark_order_received,
    name="customer_mark_order_received",
),
    path(
    "my-orders/status-api/",
    views.customer_orders_status_api,
    name="customer_orders_status_api",
),
]
