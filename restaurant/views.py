"""
Views for the restaurant application.

Public pages:
- Home
- Menu (with category/tag/search filtering)
- About
- Contact (stores messages)
- Reservation
- Delivery (location + order + checkout)

Admin/Staff pages (custom, NOT Django admin UI):
- Admin Login
- Dashboard
- Add/Edit/Delete Menu Item
- Category management
- Reservations management
- Promotions placeholders

All admin pages require login via Django auth.
"""

from __future__ import annotations

import json
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .utils import haversine_km, delivery_fee_for_distance
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import ensure_csrf_cookie, csrf_exempt
from django.db.models.deletion import ProtectedError
import re
from .models import DeliveryPricing

from .models import Review
from .forms import ReviewForm

from .forms import (
    AdminLoginForm,
    CategoryForm,
    ContactForm,
    MenuItemForm,
    ReservationForm,
    DeliveryCouponForm,
    HeroBannerForm,
    AddonGroupForm,
    AddonOptionForm,
    MenuItemAddonGroupForm,
)
from .models import (
    Category,
    DeliveryCoupon,
    DeliveryOrder,
    DeliveryOrderItem,
    DeliveryOrderItemAddon,
    DeliveryPromotion,
    MenuItem,
    Reservation,
    ReservationItem,
    HeroBanner,
    AddonGroup,
    AddonOption,
    MenuItemAddonGroup,
    TelegramLog,
)
from django.db.models import Avg, Count
from django.core.paginator import Paginator
from django.core.mail import send_mail

from .telegram_utils import (
    answer_callback_query,
    build_delivery_order_message,
    build_delivery_status_keyboard,
    delivery_status_label,
    edit_telegram_message_text,
    send_telegram_message_full,
    telegram_user_is_allowed,
)

from django.utils.translation import gettext as _



from django.core.cache import cache
from django.shortcuts import redirect

# -------------------------
# Public pages
# -------------------------


@ensure_csrf_cookie
def home(request: HttpRequest) -> HttpResponse:
    popular_items = (
        MenuItem.objects.filter(status=MenuItem.STATUS_ACTIVE)
        .filter(tags__icontains="popular")
        .select_related("category")
        .order_by("-created_at")[:4]
    )

    categories = Category.objects.filter(is_active=True)

    hero_banners = HeroBanner.objects.filter(is_active=True)

    return render(
        request,
        "home.html",
        {
            "popular_items": popular_items,
            "categories": categories,
            "hero_banners": hero_banners,
        },
    )


@ensure_csrf_cookie
def menu(request: HttpRequest) -> HttpResponse:
    """Display the menu (category + search only)."""
    categories = Category.objects.filter(is_active=True).order_by("order", "name")

    category_slug = (request.GET.get("category") or "").strip()
    q = (request.GET.get("q") or "").strip()

    items = (
        MenuItem.objects.select_related("category")
        .prefetch_related("addon_group_links")
        .exclude(status=MenuItem.STATUS_HIDDEN)
    )

    current_category: Category | None = None
    if category_slug:
        current_category = get_object_or_404(
            Category, slug=category_slug, is_active=True
        )
        items = items.filter(category=current_category)

    if q:
        items = items.filter(
            Q(name__icontains=q)
            | Q(description__icontains=q)
            | Q(category__name__icontains=q)
        )

    items = items.order_by("category__order", "category__name", "name")

    most_ordered = (
        MenuItem.objects.select_related("category")
        .prefetch_related("addon_group_links")
        .exclude(status=MenuItem.STATUS_HIDDEN)
        .filter(tags__icontains="popular")
        .order_by("-created_at")[:3]
    )

    return render(
        request,
        "menu.html",
        {
            "categories": categories,
            "items": items,
            "most_ordered": most_ordered,
            "current_category": current_category,
            "q": q,
            "tag_filter": "",
            "tag_choices": [],
            # ✅ REQUIRED for Google Maps loader in menu.html
            "GOOGLE_MAPS_API_KEY": getattr(settings, "GOOGLE_MAPS_API_KEY", ""),
            # ✅ Needed because your JS reads {{ rest_lat }} / {{ rest_lng }} in menu.html
            "rest_lat": getattr(settings, "RESTAURANT_LAT", 0),
            "rest_lng": getattr(settings, "RESTAURANT_LNG", 0),
            # Optional (if you show it in the modal)
            "max_radius": getattr(settings, "DELIVERY_MAX_RADIUS_KM", 10.0),
        },
    )


def about(request: HttpRequest) -> HttpResponse:
    """About page with a contact form section."""
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Thank you! Your message has been sent.")
            return redirect(reverse("restaurant:about"))
    else:
        form = ContactForm()
    return render(request, "about.html", {"form": form})


def contact(request: HttpRequest) -> HttpResponse:
    """Display a contact form and handle submissions."""
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(
                request, "Thank you for your message! We'll get back to you soon."
            )
            return redirect(reverse("restaurant:contact"))
    else:
        form = ContactForm()
    return render(request, "contact.html", {"form": form})


# views.py


def menu_item_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Partial template for menu/reservation modal."""
    item = get_object_or_404(
        MenuItem.objects.select_related("category"),
        pk=pk,
    )
    if item.status == MenuItem.STATUS_HIDDEN:
        return HttpResponse(status=404)

    ctx = (request.GET.get("ctx") or "menu").strip().lower()
    if ctx not in {"menu", "reservation", "delivery"}:
        ctx = "menu"

    template = "partials/menu_item_modal.html"
    if ctx == "reservation":
        template = "partials/reservation_item_modal.html"

    addon_links = []
    if ctx in {"menu", "delivery"}:
        addon_links = (
            MenuItemAddonGroup.objects.filter(
                menu_item=item,
                addon_group__is_active=True,
            )
            .select_related("addon_group")
            .prefetch_related("addon_group__options")
            .order_by("order", "id")
        )

    prepared_addon_groups = []
    for link in addon_links:
        group = link.addon_group
        options = [opt for opt in group.options.all() if opt.is_active]

        prepared_addon_groups.append(
            {
                "link_id": link.id,
                "group_id": group.id,
                "group_name": group.name,
                "group_slug": group.slug,
                "selection_type": group.selection_type,
                "selection_type_display": group.get_selection_type_display(),
                "is_required": link.effective_is_required,
                "min_select": link.effective_min_select,
                "max_select": link.effective_max_select,
                "free_choices_count": int(getattr(group, "free_choices_count", 0) or 0),
                "order": link.order,
                "options": options,
            }
        )

    return render(
        request,
        template,
        {
            "item": item,
            "ctx": ctx,
            "addon_groups": prepared_addon_groups,
        },
    )


# -------------------------
# Custom admin pages
# -------------------------


def admin_login(request: HttpRequest) -> HttpResponse:
    """Custom admin login page using Django authentication."""
    if request.user.is_authenticated:
        return redirect(reverse("restaurant:dashboard"))

    if request.method == "POST":
        form = AdminLoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)

            remember = form.cleaned_data.get("remember_me")
            if not remember:
                request.session.set_expiry(0)

            return redirect(reverse("restaurant:dashboard"))
    else:
        form = AdminLoginForm(request)

    return render(request, "admin/custom_login.html", {"form": form})


@login_required
def admin_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect(reverse("restaurant:admin_login"))


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    total_items = MenuItem.objects.count()
    total_categories = Category.objects.count()
    sold_out_count = MenuItem.objects.filter(status=MenuItem.STATUS_SOLD_OUT).count()

    recent_items = MenuItem.objects.select_related("category").order_by("-created_at")[
        :50
    ]
    categories = Category.objects.all().order_by("order", "name")

    total_reservations = Reservation.objects.count()
    pending_reservations = Reservation.objects.filter(
        status=Reservation.STATUS_PENDING
    ).count()
    upcoming_reservations = Reservation.objects.filter(
        start_datetime__gte=timezone.now()
    ).count()
    recent_reservations = Reservation.objects.order_by("-created_at")[:50]

    pending_delivery_orders = DeliveryOrder.objects.filter(status="pending").count()
    completed_delivery_orders = DeliveryOrder.objects.filter(status="delivered").count()
    active_items_count = MenuItem.objects.filter(status=MenuItem.STATUS_ACTIVE).count()
    recent_delivery_orders = DeliveryOrder.objects.order_by("-created_at")[:50]

    context = {
        "total_items": total_items,
        "total_categories": total_categories,
        "sold_out_count": sold_out_count,
        "recent_items": recent_items,
        "categories": categories,
        "total_reservations": total_reservations,
        "pending_reservations": pending_reservations,
        "upcoming_reservations": upcoming_reservations,
        "recent_reservations": recent_reservations,
        "pending_delivery_orders": pending_delivery_orders,
        "completed_delivery_orders": completed_delivery_orders,
        "active_items_count": active_items_count,
        "recent_delivery_orders": recent_delivery_orders,
    }
    return render(request, "admin/dashboard.html", context)


@login_required
def add_menu_item(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = MenuItemForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, _("Menu item created successfully."))
            return redirect(reverse("restaurant:dashboard"))
    else:
        form = MenuItemForm()
    return render(request, "admin/add_item.html", {"form": form})


@login_required
def edit_menu_item(request: HttpRequest, pk: int) -> HttpResponse:
    item = get_object_or_404(MenuItem, pk=pk)
    if request.method == "POST":
        form = MenuItemForm(request.POST, request.FILES, instance=item)
        if form.is_valid():
            form.save()
            messages.success(request, "Menu item updated.")
            return redirect(reverse("restaurant:dashboard"))
    else:
        form = MenuItemForm(instance=item)
    return render(request, "admin/edit_item.html", {"form": form, "item": item})


@login_required
def delete_menu_item(request, pk):
    item = get_object_or_404(MenuItem, pk=pk)

    if request.method == "POST":
        try:
            item.delete()
            messages.success(request, "Menu item deleted.")
            return redirect(reverse("restaurant:dashboard"))

        except ProtectedError:
            # cannot delete because used in ReservationItem
            item.status = MenuItem.STATUS_HIDDEN
            item.save(update_fields=["status"])
            messages.warning(
                request,
                "This item cannot be deleted because it was used in reservations/orders. "
                "It has been hidden instead.",
            )
            return redirect(reverse("restaurant:dashboard"))

    return render(request, "admin/delete_item.html", {"item": item})


@login_required
def categories_list(request: HttpRequest) -> HttpResponse:
    categories = Category.objects.all().order_by("order", "name")
    return render(request, "admin/categories.html", {"categories": categories})


@login_required
def add_category(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Category created.")
            return redirect(reverse("restaurant:categories_list"))
    else:
        form = CategoryForm()
    return render(request, "admin/add_category.html", {"form": form})


@login_required
def edit_category(request: HttpRequest, pk: int) -> HttpResponse:
    category = get_object_or_404(Category, pk=pk)
    if request.method == "POST":
        form = CategoryForm(request.POST, instance=category)
        if form.is_valid():
            form.save()
            messages.success(request, "Category updated.")
            return redirect(reverse("restaurant:categories_list"))
    else:
        form = CategoryForm(instance=category)
    return render(
        request, "admin/edit_category.html", {"form": form, "category": category}
    )


@login_required
def delete_category(request: HttpRequest, pk: int) -> HttpResponse:
    category = get_object_or_404(Category, pk=pk)
    has_items = category.menu_items.exists()

    if request.method == "POST":
        if has_items:
            messages.error(
                request, "Cannot delete category while it contains menu items."
            )
            return redirect(reverse("restaurant:categories_list"))
        category.delete()
        messages.success(request, "Category deleted.")
        return redirect(reverse("restaurant:categories_list"))

    return render(
        request,
        "admin/delete_category.html",
        {"category": category, "has_items": has_items},
    )


@login_required
def menu_items_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    category_slug = (request.GET.get("category") or "").strip()
    status = (request.GET.get("status") or "").strip()

    items = MenuItem.objects.select_related("category").all().order_by("-created_at")

    if q:
        items = items.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if category_slug:
        items = items.filter(category__slug=category_slug)
    if status:
        items = items.filter(status=status)

    categories = Category.objects.all().order_by("order", "name")

    return render(
        request,
        "admin/menu_items.html",
        {
            "items": items,
            "categories": categories,
            "q": q,
            "category_slug": category_slug,
            "status": status,
        },
    )


@require_POST
@login_required
def menu_items_bulk_update(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("item_ids")
    new_status = (request.POST.get("new_status") or "").strip()

    valid = {MenuItem.STATUS_ACTIVE, MenuItem.STATUS_SOLD_OUT, MenuItem.STATUS_HIDDEN}
    if new_status not in valid:
        messages.error(request, "Please choose a valid status.")
        return redirect("restaurant:menu_items_list")

    ids_int = [int(x) for x in ids if str(x).isdigit()]
    if not ids_int:
        messages.error(request, "No items selected.")
        return redirect("restaurant:menu_items_list")

    updated = MenuItem.objects.filter(id__in=ids_int).update(status=new_status)
    messages.success(request, f"Updated {updated} item(s).")
    return redirect("restaurant:menu_items_list")


@require_POST
@login_required
def menu_items_bulk_delete(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("item_ids")
    ids_int = [int(x) for x in ids if str(x).isdigit()]

    if not ids_int:
        messages.error(request, "No items selected.")
        return redirect("restaurant:menu_items_list")

    deleted = 0
    hidden = 0

    for pk in ids_int:
        item = MenuItem.objects.filter(pk=pk).first()
        if not item:
            continue
        try:
            item.delete()
            deleted += 1
        except ProtectedError:
            # used in ReservationItem / etc -> hide instead
            item.status = MenuItem.STATUS_HIDDEN
            item.save(update_fields=["status"])
            hidden += 1

    if deleted and hidden:
        messages.success(
            request, f"Deleted {deleted} item(s), hidden {hidden} item(s) (in use)."
        )
    elif deleted:
        messages.success(request, f"Deleted {deleted} item(s).")
    elif hidden:
        messages.success(request, f"Hidden {hidden} item(s) (in use).")
    else:
        messages.info(request, "No changes made.")

    return redirect("restaurant:menu_items_list")


# -------------------------
# Addon management (custom admin)
# -------------------------


@login_required
def addon_groups_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    groups = AddonGroup.objects.all().order_by("order", "name")

    if q:
        groups = groups.filter(Q(name__icontains=q) | Q(slug__icontains=q))

    if status == "active":
        groups = groups.filter(is_active=True)
    elif status == "inactive":
        groups = groups.filter(is_active=False)

    return render(
        request,
        "admin/addon_groups.html",
        {
            "groups": groups,
            "q": q,
            "status": status,
        },
    )


@login_required
def addon_group_add(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = AddonGroupForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Addon group '{obj.name}' created.")
            return redirect("restaurant:addon_groups_list")
    else:
        form = AddonGroupForm()

    return render(
        request,
        "admin/addon_group_form.html",
        {
            "form": form,
            "mode": "add",
        },
    )


@login_required
def addon_group_edit(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(AddonGroup, pk=pk)

    if request.method == "POST":
        form = AddonGroupForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Addon group '{obj.name}' updated.")
            return redirect("restaurant:addon_groups_list")
    else:
        form = AddonGroupForm(instance=obj)

    return render(
        request,
        "admin/addon_group_form.html",
        {
            "form": form,
            "mode": "edit",
            "obj": obj,
        },
    )


@login_required
def addon_group_delete(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(AddonGroup, pk=pk)
    linked_items_count = obj.menu_item_links.count()
    options_count = obj.options.count()

    if request.method == "POST":
        name = obj.name
        obj.delete()
        messages.success(request, f"Addon group '{name}' deleted.")
        return redirect("restaurant:addon_groups_list")

    return render(
        request,
        "admin/addon_group_delete.html",
        {
            "obj": obj,
            "linked_items_count": linked_items_count,
            "options_count": options_count,
        },
    )


@login_required
def addon_options_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    group_id = (request.GET.get("group") or "").strip()
    status = (request.GET.get("status") or "").strip()

    options = AddonOption.objects.select_related("group").all().order_by(
        "group__order", "group__name", "order", "name"
    )

    if q:
        options = options.filter(
            Q(name__icontains=q) | Q(group__name__icontains=q)
        )

    if group_id.isdigit():
        options = options.filter(group_id=int(group_id))

    if status == "active":
        options = options.filter(is_active=True)
    elif status == "inactive":
        options = options.filter(is_active=False)

    groups = AddonGroup.objects.all().order_by("order", "name")

    return render(
        request,
        "admin/addon_options.html",
        {
            "options": options,
            "groups": groups,
            "q": q,
            "group_id": group_id,
            "status": status,
        },
    )


@login_required
def addon_option_add(request: HttpRequest) -> HttpResponse:
    initial = {}
    group_id = (request.GET.get("group") or "").strip()
    if group_id.isdigit():
        initial["group"] = int(group_id)

    if request.method == "POST":
        form = AddonOptionForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Addon option '{obj.name}' created.")
            return redirect("restaurant:addon_options_list")
    else:
        form = AddonOptionForm(initial=initial)

    return render(
        request,
        "admin/addon_option_form.html",
        {
            "form": form,
            "mode": "add",
        },
    )


@login_required
def addon_option_edit(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(AddonOption, pk=pk)

    if request.method == "POST":
        form = AddonOptionForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            messages.success(request, f"Addon option '{obj.name}' updated.")
            return redirect("restaurant:addon_options_list")
    else:
        form = AddonOptionForm(instance=obj)

    return render(
        request,
        "admin/addon_option_form.html",
        {
            "form": form,
            "mode": "edit",
            "obj": obj,
        },
    )


@login_required
def addon_option_delete(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(AddonOption.objects.select_related("group"), pk=pk)

    if request.method == "POST":
        name = obj.name
        obj.delete()
        messages.success(request, f"Addon option '{name}' deleted.")
        return redirect("restaurant:addon_options_list")

    return render(
        request,
        "admin/addon_option_delete.html",
        {
            "obj": obj,
        },
    )


@login_required
def menu_item_addon_links_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    item_id = (request.GET.get("item") or "").strip()
    group_id = (request.GET.get("group") or "").strip()

    links = (
        MenuItemAddonGroup.objects.select_related("menu_item", "menu_item__category", "addon_group")
        .all()
        .order_by("menu_item__category__order", "menu_item__category__name", "menu_item__name", "order", "id")
    )

    if q:
        links = links.filter(
            Q(menu_item__name__icontains=q)
            | Q(addon_group__name__icontains=q)
            | Q(menu_item__category__name__icontains=q)
        )

    if item_id.isdigit():
        links = links.filter(menu_item_id=int(item_id))

    if group_id.isdigit():
        links = links.filter(addon_group_id=int(group_id))

    menu_items = (
        MenuItem.objects.exclude(status=MenuItem.STATUS_HIDDEN)
        .select_related("category")
        .order_by("category__order", "category__name", "name")
    )
    groups = AddonGroup.objects.all().order_by("order", "name")

    return render(
        request,
        "admin/menu_item_addon_links.html",
        {
            "links": links,
            "menu_items": menu_items,
            "groups": groups,
            "q": q,
            "item_id": item_id,
            "group_id": group_id,
        },
    )


@login_required
def menu_item_addon_link_add(request: HttpRequest) -> HttpResponse:
    initial = {}
    item_id = (request.GET.get("item") or "").strip()
    if item_id.isdigit():
        initial["menu_item"] = int(item_id)

    if request.method == "POST":
        form = MenuItemAddonGroupForm(request.POST)
        if form.is_valid():
            obj = form.save()
            messages.success(
                request,
                f"Addon group '{obj.addon_group.name}' assigned to '{obj.menu_item.name}'.",
            )
            return redirect("restaurant:menu_item_addon_links_list")
    else:
        form = MenuItemAddonGroupForm(initial=initial)

    return render(
        request,
        "admin/menu_item_addon_link_form.html",
        {
            "form": form,
            "mode": "add",
        },
    )


@login_required
def menu_item_addon_link_edit(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(
        MenuItemAddonGroup.objects.select_related("menu_item", "addon_group"),
        pk=pk,
    )

    if request.method == "POST":
        form = MenuItemAddonGroupForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save()
            messages.success(
                request,
                f"Addon assignment updated for '{obj.menu_item.name}'.",
            )
            return redirect("restaurant:menu_item_addon_links_list")
    else:
        form = MenuItemAddonGroupForm(instance=obj)

    return render(
        request,
        "admin/menu_item_addon_link_form.html",
        {
            "form": form,
            "mode": "edit",
            "obj": obj,
        },
    )


@login_required
def menu_item_addon_link_delete(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(
        MenuItemAddonGroup.objects.select_related("menu_item", "addon_group"),
        pk=pk,
    )

    if request.method == "POST":
        menu_name = obj.menu_item.name
        group_name = obj.addon_group.name
        obj.delete()
        messages.success(
            request,
            f"Removed addon group '{group_name}' from '{menu_name}'.",
        )
        return redirect("restaurant:menu_item_addon_links_list")

    return render(
        request,
        "admin/menu_item_addon_link_delete.html",
        {
            "obj": obj,
        },
    )


@login_required
def reservations_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    qs = Reservation.objects.all().order_by("-start_datetime", "-created_at")
    if q:
        qs = qs.filter(
            Q(name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q)
        )
    if status:
        qs = qs.filter(status=status)

    return render(
        request,
        "admin/reservations.html",
        {
            "reservations": qs[:300],
            "q": q,
            "status": status,
            "status_choices": Reservation.STATUS_CHOICES,
        },
    )


@login_required
def reservation_detail_admin(request: HttpRequest, pk: int) -> HttpResponse:
    r = get_object_or_404(
        Reservation.objects.prefetch_related("items__menu_item"), pk=pk
    )
    return render(request, "admin/reservation_detail.html", {"r": r})


@login_required
def reservation_update_status(request: HttpRequest, pk: int) -> HttpResponse:
    r = get_object_or_404(Reservation, pk=pk)
    if request.method == "POST":
        new_status = (request.POST.get("status") or "").strip()
        valid = {k for k, _ in Reservation.STATUS_CHOICES}
        if new_status in valid:
            r.status = new_status
            r.save(update_fields=["status"])
            messages.success(request, "Reservation status updated.")
        else:
            messages.error(request, "Invalid status.")
    return redirect(reverse("restaurant:reservation_detail_admin", args=[pk]))


@require_POST
@login_required
def reservations_bulk_update(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("reservation_ids")
    new_status = (request.POST.get("new_status") or "").strip()

    valid = {k for k, _ in Reservation.STATUS_CHOICES}
    if new_status not in valid:
        messages.error(request, "Please choose a valid status.")
        return redirect("restaurant:reservations_list")

    ids_int = [int(x) for x in ids if str(x).isdigit()]
    if not ids_int:
        messages.error(request, "No reservations selected.")
        return redirect("restaurant:reservations_list")

    updated = Reservation.objects.filter(id__in=ids_int).update(status=new_status)
    messages.success(request, f"Updated {updated} reservation(s).")
    return redirect("restaurant:reservations_list")


@require_POST
@login_required
def reservations_bulk_delete(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("reservation_ids")
    ids_int = [int(x) for x in ids if str(x).isdigit()]

    if not ids_int:
        messages.error(request, "No reservations selected.")
        return redirect("restaurant:reservations_list")

    deleted_count, _ = Reservation.objects.filter(id__in=ids_int).delete()
    messages.success(request, f"Deleted {deleted_count} record(s).")
    return redirect("restaurant:reservations_list")

def reservation(request: HttpRequest) -> HttpResponse:
    """Public reservation page (with optional pre-order items)."""

    categories = Category.objects.filter(is_active=True).order_by("order", "name")
    menu_items = (
        MenuItem.objects.select_related("category")
        .exclude(status=MenuItem.STATUS_HIDDEN)
        .order_by("category__order", "category__name", "name")
    )

    reservation_obj = None
    show_modal = False

    if request.method == "POST":
        form = ReservationForm(request.POST)

        if form.is_valid():
            with transaction.atomic():
                r = form.save(commit=False)
                r.user = request.user if request.user.is_authenticated else None
                r.save()

                # ---- Handle pre-order items ----
                preorder_ids = request.POST.getlist("preorder_ids")
                preorder_qty = request.POST.getlist("preorder_qty")

                bulk = []
                for mid, qty in zip(preorder_ids, preorder_qty):
                    if not str(mid).isdigit():
                        continue
                    qty_i = int(qty) if str(qty).isdigit() else 0
                    if qty_i <= 0:
                        continue

                    mi = (
                        MenuItem.objects.filter(id=int(mid))
                        .exclude(status=MenuItem.STATUS_HIDDEN)
                        .first()
                    )
                    if not mi:
                        continue

                    bulk.append(
                        ReservationItem(
                            reservation=r,
                            menu_item=mi,
                            qty=qty_i,
                            unit_price=mi.price,
                        )
                    )

                if bulk:
                    ReservationItem.objects.bulk_create(bulk)

            # ✅ store last reservation id in session (for success modal)
            request.session["last_reservation_id"] = r.id
            request.session.modified = True

            # --- Telegram notify (safe: never breaks reservation) ---
            try:
                from restaurant.telegram_utils import send_telegram_message
                from django.utils import timezone

                dt_local = timezone.localtime(r.start_datetime)
                when = dt_local.strftime("%d %b %Y, %I:%M %p")

                preorder_lines = [
                    f"• {it.menu_item.name} × {it.qty} = € {(it.unit_price * it.qty):.2f}"
                    for it in r.items.select_related("menu_item").all()
                ]
                preorder_text = "\n".join(preorder_lines) if preorder_lines else "—"

                msg = (
                    f"📅 NEW RESERVATION\n\n"
                    f"ID: #{r.id}\n"
                    f"Name: {r.name}\n"
                    f"Phone: {r.phone}\n"
                    f"Email: {r.email or '-'}\n"
                    f"Date & Time: {when}\n"
                    f"Party size: {r.party_size}\n"
                    f"Baby seats: {r.baby_seats}\n"
                    f"Preferred table: {r.preferred_table or '-'}\n"
                    f"Notes: {r.notes or '-'}\n\n"
                    f"Pre-order:\n{preorder_text}"
                )

                send_telegram_message(msg, kind="reservation")

            except Exception as e:
                try:
                    from restaurant.models import TelegramLog
                    TelegramLog.objects.create(
                        ok=False,
                        kind="reservation",
                        chat_id=str(getattr(settings, "TELEGRAM_GROUP_CHAT_ID", "")),
                        message_preview="reservation telegram failed",
                        response_text=repr(e),
                    )
                except Exception:
                    pass

            return redirect(reverse("restaurant:reservation") + "?placed=1")

    else:
        form = ReservationForm()

    # -------------------------
    # SUCCESS MODAL LOGIC
    # -------------------------
    placed = request.GET.get("placed") == "1"
    if placed:
        last_id = request.session.get("last_reservation_id")
        if last_id:
            reservation_obj = (
                Reservation.objects.prefetch_related("items__menu_item")
                .filter(id=last_id)
                .first()
            )
            show_modal = bool(reservation_obj)

            # remove so it only shows once
            request.session.pop("last_reservation_id", None)
            request.session.modified = True

    return render(
        request,
        "reservation.html",
        {
            "form": form,
            "categories": categories,
            "menu_items": menu_items,
            "show_reservation_modal": show_modal,
            "reservation_obj": reservation_obj,
        },
    )
    
    
    

# -------------------------
# Telegram delivery order controls
# -------------------------

def _allowed_delivery_status_targets(current_status: str) -> set[str]:
    current_status = str(current_status or "").strip()

    mapping = {
        DeliveryOrder.STATUS_PENDING: {
            DeliveryOrder.STATUS_ACCEPTED,
            DeliveryOrder.STATUS_CANCELLED,
        },
        DeliveryOrder.STATUS_ACCEPTED: {
            DeliveryOrder.STATUS_PREPARING,
        },
        DeliveryOrder.STATUS_PREPARING: {
            DeliveryOrder.STATUS_OUT_FOR_DELIVERY,
        },
        DeliveryOrder.STATUS_OUT_FOR_DELIVERY: {
            DeliveryOrder.STATUS_DELIVERED,
        },
        DeliveryOrder.STATUS_DELIVERED: set(),
        DeliveryOrder.STATUS_CANCELLED: set(),
    }
    return mapping.get(current_status, set())


def _telegram_status_change_is_valid(current_status: str, target_status: str) -> bool:
    return target_status in _allowed_delivery_status_targets(current_status)


def send_delivery_status_email(order: DeliveryOrder) -> None:
    """
    Send status update email to the customer.
    Safe helper: never breaks existing order flow.
    """
    try:
        if not order:
            return

        recipient = ""
        if getattr(order, "user", None) and getattr(order.user, "email", ""):
            recipient = order.user.email.strip()

        if not recipient:
            return

        status_label = order.get_status_display()
        estimated_time = ""

        if order.status == DeliveryOrder.STATUS_ACCEPTED:
            estimated_time = "Estimated time: 45–60 minutes."
        elif order.status == DeliveryOrder.STATUS_PREPARING:
            estimated_time = "Estimated time: 30–45 minutes."
        elif order.status == DeliveryOrder.STATUS_OUT_FOR_DELIVERY:
            estimated_time = "Estimated time: 10–20 minutes."
        elif order.status == DeliveryOrder.STATUS_DELIVERED:
            estimated_time = "Your food has been delivered."
        elif order.status == DeliveryOrder.STATUS_CANCELLED:
            estimated_time = "Your order has been cancelled."
        else:
            estimated_time = "We will keep you updated."

        subject = f"Order #{order.id} status updated – {status_label}"

        message = (
            f"Hello {order.customer_name},\n\n"
            f"Your order #{order.id} status has been updated.\n\n"
            f"New status: {status_label}\n"
            f"{estimated_time}\n\n"
            f"Delivery address: {order.address_label}\n"
            f"Order total: € {order.total:.2f}\n\n"
            f"Thank you for ordering from {settings.RESTAURANT_NAME}."
        )

        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            fail_silently=True,
        )

    except Exception:
        pass


# -------------------------
# Delivery (location + calc + order + checkout)
# -------------------------


def _active_promo() -> DeliveryPromotion | None:
    promo = (
        DeliveryPromotion.objects.filter(is_active=True).order_by("-created_at").first()
    )
    if promo and promo.is_current():
        return promo
    return None


def _apply_promo_delivery_fee(fee: float, subtotal: float) -> tuple[float, dict]:
    promo = _active_promo()
    if not promo:
        return float(fee), {"active": False}

    min_sub = float(promo.min_subtotal or 0)
    ok_min = subtotal >= min_sub

    return float(fee), {
        "active": True,
        "title": promo.title,
        "free_delivery": False,  # force false
        "min_subtotal": min_sub,
    }


from datetime import datetime
import secrets

# -------------------------
# Loyalty config loader
# -------------------------

def _loyalty_config():
    from .models import LoyaltyProgram

    obj = LoyaltyProgram.objects.first()
    if not obj or not obj.is_active:
        return {
            "enabled": False,
            "target": 10,
            "percent": 30,
        }

    return {
        "enabled": True,
        "target": int(obj.target_orders or 10),
        "percent": int(obj.reward_percent or 30),
    }

def _month_bounds(now=None):
    now = now or timezone.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # next month
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _issued_month_str(now=None) -> str:
    now = now or timezone.now()
    return now.strftime("%Y-%m")


def _loyalty_delivered_count(user) -> int:
    if not user or not user.is_authenticated:
        return 0
    start, end = _month_bounds()
    return DeliveryOrder.objects.filter(
        user=user,
        status=DeliveryOrder.STATUS_DELIVERED,
        created_at__gte=start,
        created_at__lt=end,
    ).count()


def _ensure_loyalty_coupon_for_user(user) -> DeliveryCoupon | None:
    if not user or not user.is_authenticated:
        return None

    cfg = _loyalty_config()
    if not cfg["enabled"]:
        return None

    target = int(cfg["target"] or 10)
    percent = int(cfg["percent"] or 30)

    count = _loyalty_delivered_count(user)
    if count < target:
        return None

    month_key = _issued_month_str()

    existing = DeliveryCoupon.objects.filter(
        is_active=True,
        is_personal=True,
        assigned_user=user,
        issued_month=month_key,
        discount_type=DeliveryCoupon.DISCOUNT_PERCENT,
        discount_value=Decimal(str(percent)),
    ).first()

    if existing and existing.is_current():
        return existing

    # create a new one-time coupon for this month
    code = f"LOYAL{percent}-" + secrets.token_hex(3).upper()

    start, end = _month_bounds()
    coupon = DeliveryCoupon.objects.create(
        code=code,
        is_active=True,
        discount_type=DeliveryCoupon.DISCOUNT_PERCENT,
        discount_value=Decimal(str(percent)),
        min_subtotal=Decimal("0"),
        start_at=start,
        end_at=end,
        max_uses=1,
        used_count=0,
        # personal loyalty
        is_personal=True,
        assigned_user=user,
        issued_month=month_key,
    )
    return coupon
def _loyalty_ui_context(user) -> dict:
    """
    What you show to the user as notification/progress.
    """
    if not user or not user.is_authenticated:
        return {"enabled": False}

    cfg = _loyalty_config()
    if not cfg["enabled"]:
        return {"enabled": False}

    target = int(cfg["target"] or 10)
    percent = int(cfg["percent"] or 30)

    count = _loyalty_delivered_count(user)
    remaining = max(0, target - count)

    coupon = None
    if count >= target:
        coupon = _ensure_loyalty_coupon_for_user(user)

    return {
        "enabled": True,
        "count": count,
        "target": target,
        "remaining": remaining,
        "percent": percent,
        "earned": bool(coupon),
        "coupon_code": coupon.code if coupon else "",
        "month": _issued_month_str(),
    }

# -------------------------
# Coupon helpers
# -------------------------


def _get_coupon_from_session(request: HttpRequest) -> DeliveryCoupon | None:
    code = (request.session.get("delivery_coupon_code") or "").strip()
    if not code:
        return None

    coupon = DeliveryCoupon.objects.filter(code__iexact=code).first()
    if not coupon or not coupon.is_current():
        return None  # ✅ never redirect from helper

    # ✅ If it’s a personal coupon, enforce login + ownership
    if getattr(coupon, "is_personal", False):
        if not request.user.is_authenticated:
            return None

        if coupon.assigned_user_id and coupon.assigned_user_id != request.user.id:
            return None

    # ✅ Normal coupons come here too
    return coupon
    # ✅ block using someone else’s personal coupon
    if getattr(coupon, "is_personal", False):
        if not request.user.is_authenticated:
            msg = "Please log in to use this coupon."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return _back()

        if coupon.assigned_user_id and coupon.assigned_user_id != request.user.id:
            msg = "This coupon is not for your account."
            if request.headers.get("X-Requested-With") == "XMLHttpRequest":
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return _back()


def _coupon_discount_for_request(
    request: HttpRequest, subtotal: float
) -> tuple[float, dict]:
    coupon = _get_coupon_from_session(request)
    if not coupon:
        return 0.0, {"active": False}

    # Free delivery coupon: no subtotal discount, but still "active"
    if coupon.discount_type == DeliveryCoupon.DISCOUNT_FREE_DELIVERY:
        ok = coupon.grants_free_delivery(Decimal(str(subtotal)))
        return 0.0, {
            "active": True,
            "code": coupon.code,
            "type": coupon.discount_type,
            "value": 0,
            "min_subtotal": float(coupon.min_subtotal or 0),
            "free_delivery": bool(ok),
        }

    # Percent / fixed coupon:
    disc = coupon.compute_discount(Decimal(str(subtotal)))
    disc_f = float(disc)

    # ✅ IMPORTANT: if subtotal is 0, keep coupon ACTIVE for UI preview
    if float(subtotal) <= 0:
        return 0.0, {
            "active": True,
            "code": coupon.code,
            "type": coupon.discount_type,
            "value": float(coupon.discount_value or 0),
            "min_subtotal": float(coupon.min_subtotal or 0),
            "free_delivery": False,
        }

    # If subtotal > 0 but discount still 0, treat as inactive
    if disc_f <= 0:
        return 0.0, {"active": False}

    return disc_f, {
        "active": True,
        "code": coupon.code,
        "discount": round(disc_f, 2),
        "type": coupon.discount_type,
        "value": float(coupon.discount_value or 0),
        "min_subtotal": float(coupon.min_subtotal or 0),
        "free_delivery": False,
    }




# --- Session cart (ONE system only) ---
def _normalize_selected_option_ids(raw_ids) -> list[int]:
    out: list[int] = []
    seen = set()

    for raw in raw_ids or []:
        try:
            oid = int(raw)
        except Exception:
            continue
        if oid <= 0 or oid in seen:
            continue
        seen.add(oid)
        out.append(oid)

    return sorted(out)


def _build_cart_line_key(item_id: int, option_ids: list[int]) -> str:
    if not option_ids:
        return str(item_id)
    suffix = "-".join(str(x) for x in sorted(option_ids))
    return f"{item_id}:{suffix}"


def _get_item_addon_links(item: MenuItem):
    return (
        MenuItemAddonGroup.objects.filter(
            menu_item=item,
            addon_group__is_active=True,
        )
        .select_related("addon_group")
        .prefetch_related("addon_group__options")
        .order_by("order", "id")
    )


def _validate_selected_addons_for_item(item: MenuItem, raw_option_ids) -> tuple[list[int], list[AddonOption], list[dict]]:
    """
    Returns:
      normalized_ids, selected_options, errors
    errors format:
      [{"group_id": 1, "message": "..."}]
    """
    option_ids = _normalize_selected_option_ids(raw_option_ids)
    selected_options = list(
        AddonOption.objects.select_related("group")
        .filter(id__in=option_ids, is_active=True, group__is_active=True)
    )
    selected_map = {opt.id: opt for opt in selected_options}

    links = list(_get_item_addon_links(item))
    if not links:
        # item has no addon groups -> no selected addon is allowed
        if option_ids:
            return [], [], [{"group_id": "", "message": "This item does not allow addon selections."}]
        return [], [], []

    allowed_group_ids = {link.addon_group_id for link in links}
    group_selected: dict[int, list[AddonOption]] = {}

    invalid_option_found = False
    for oid in option_ids:
        opt = selected_map.get(oid)
        if not opt:
            invalid_option_found = True
            continue
        if opt.group_id not in allowed_group_ids:
            invalid_option_found = True
            continue
        group_selected.setdefault(opt.group_id, []).append(opt)

    errors: list[dict] = []

    if invalid_option_found:
        errors.append({"group_id": "", "message": "Invalid addon selection."})

    for link in links:
        group = link.addon_group
        chosen = group_selected.get(group.id, [])

        min_select = link.effective_min_select
        max_select = link.effective_max_select
        is_required = link.effective_is_required

        if is_required and len(chosen) == 0:
            errors.append({
                "group_id": group.id,
                "message": "Please choose at least one option.",
            })
            continue

        if len(chosen) < min_select:
            errors.append({
                "group_id": group.id,
                "message": f"Please select at least {min_select} option(s).",
            })

        if max_select is not None and len(chosen) > max_select:
            errors.append({
                "group_id": group.id,
                "message": f"Please select no more than {max_select} option(s).",
            })

        if group.selection_type == AddonGroup.SELECTION_SINGLE and len(chosen) > 1:
            errors.append({
                "group_id": group.id,
                "message": "Please choose only one option.",
            })

    normalized_valid_ids = sorted(
        opt.id for opts in group_selected.values() for opt in opts
    )

    valid_selected_options = []
    seen = set()
    for oid in normalized_valid_ids:
        opt = selected_map.get(oid)
        if opt and opt.id not in seen:
            seen.add(opt.id)
            valid_selected_options.append(opt)

    return normalized_valid_ids, valid_selected_options, errors


def _serialize_selected_addons(selected_addons: list[dict]) -> list[dict]:
    return [
        {
            "option_id": row["option"].id,
            "group_id": row["option"].group_id,
            "group_name": row["option"].group.name,
            "option_name": row["option"].name,
            "price": float(row["charged_price"]),
            "base_price": float(row["base_price"]),
            "is_free": bool(row["is_free"]),
        }
        for row in selected_addons
    ]


def _build_priced_selected_addons_for_item(item: MenuItem, selected_option_ids: list[int]) -> list[dict]:
    """
    Returns selected addons with effective charged price after applying
    the group's free_choices_count rule.

    Output rows:
    {
        "option": AddonOption,
        "base_price": Decimal,
        "charged_price": Decimal,
        "is_free": bool,
    }
    """
    if not selected_option_ids:
        return []

    selected_options = list(
        AddonOption.objects.select_related("group")
        .filter(id__in=selected_option_ids, is_active=True, group__is_active=True)
        .order_by("group__order", "group__name", "order", "id")
    )
    selected_map = {opt.id: opt for opt in selected_options}

    priced_rows: list[dict] = []

    addon_links = list(_get_item_addon_links(item))
    allowed_group_ids = {link.addon_group_id for link in addon_links}

    group_selected: dict[int, list[AddonOption]] = {}
    for oid in selected_option_ids:
        opt = selected_map.get(oid)
        if not opt:
            continue
        if opt.group_id not in allowed_group_ids:
            continue
        group_selected.setdefault(opt.group_id, []).append(opt)

    link_map = {link.addon_group_id: link for link in addon_links}

    for group_id, opts in group_selected.items():
        link = link_map.get(group_id)
        group = link.addon_group if link else None
        free_count = int(getattr(group, "free_choices_count", 0) or 0)

        for index, opt in enumerate(opts):
            base_price = Decimal(str(opt.price or 0))
            is_free = index < free_count
            charged_price = Decimal("0.00") if is_free else base_price

            priced_rows.append(
                {
                    "option": opt,
                    "base_price": base_price,
                    "charged_price": charged_price,
                    "is_free": is_free,
                }
            )

    return priced_rows

def _cart_parse_lines(request: HttpRequest) -> list[dict]:
    """
    Parses session cart and returns normalized lines with pricing.
    """
    cart = _cart_get(request.session)
    items_map = cart.get("items", {}) if isinstance(cart, dict) else {}

    line_rows = []
    item_ids: list[int] = []

    for cart_key, row in items_map.items():
        if not isinstance(row, dict):
            continue

        try:
            item_id = int(row.get("item_id"))
            qty = int(row.get("qty", 0))
        except Exception:
            continue

        if qty <= 0:
            continue

        selected_option_ids = _normalize_selected_option_ids(row.get("selected_options", []))

        item_ids.append(item_id)
        line_rows.append(
            {
                "key": str(cart_key),
                "item_id": item_id,
                "qty": qty,
                "selected_option_ids": selected_option_ids,
            }
        )

    if not line_rows:
        return []

    menu_items = {
        m.id: m
        for m in MenuItem.objects.filter(id__in=item_ids).exclude(status=MenuItem.STATUS_HIDDEN)
    }

    all_option_ids = sorted({
        oid
        for row in line_rows
        for oid in row["selected_option_ids"]
    })

    options_map = {
        opt.id: opt
        for opt in AddonOption.objects.select_related("group")
        .filter(id__in=all_option_ids, is_active=True, group__is_active=True)
    }

    parsed_lines = []

    for row in line_rows:
        item = menu_items.get(row["item_id"])
        if not item:
            continue

        priced_selected_addons = _build_priced_selected_addons_for_item(
            item,
            row["selected_option_ids"],
        )

        addons_total = sum(
            (addon["charged_price"] for addon in priced_selected_addons),
            Decimal("0.00"),
        )

        base_price = Decimal(str(item.price or 0))
        unit_price = base_price + addons_total
        qty = int(row["qty"])
        line_total = unit_price * Decimal(qty)

        parsed_lines.append(
            {
                "key": row["key"],
                "id": item.id,
                "name": item.name,
                "qty": qty,
                "base_price": round(float(base_price), 2),
                "addons_total": round(float(addons_total), 2),
                "unit_price": round(float(unit_price), 2),
                "line_total": round(float(line_total), 2),
                "addons": _serialize_selected_addons(priced_selected_addons),
            }
        )

    return parsed_lines

def _cart_get(session) -> dict:
    """
    Cart stored in session as:
    {
      "items": {
        "12": {
          "item_id": 12,
          "qty": 2,
          "selected_options": []
        },
        "12:4-8": {
          "item_id": 12,
          "qty": 1,
          "selected_options": [4, 8]
        }
      }
    }

    Also auto-upgrades old cart structure:
    { "items": { "12": {"qty": 2} } }
    """
    cart = session.get("delivery_cart")
    if not isinstance(cart, dict):
        cart = {"items": {}}
        session["delivery_cart"] = cart

    if "items" not in cart or not isinstance(cart["items"], dict):
        cart["items"] = {}
        session["delivery_cart"] = cart

    upgraded = False
    new_items = {}

    for k, v in cart["items"].items():
        if not isinstance(v, dict):
            continue

        try:
            item_id = int(v.get("item_id", k))
            qty = int(v.get("qty", 0))
        except Exception:
            continue

        if qty <= 0:
            continue

        selected_options = _normalize_selected_option_ids(v.get("selected_options", []))
        line_key = _build_cart_line_key(item_id, selected_options)

        new_items[line_key] = {
            "item_id": item_id,
            "qty": qty,
            "selected_options": selected_options,
        }

        if str(k) != line_key or "item_id" not in v or "selected_options" not in v:
            upgraded = True

    if upgraded or new_items != cart["items"]:
        cart["items"] = new_items
        session["delivery_cart"] = cart
        session.modified = True

    return cart

def _cart_subtotal(request: HttpRequest) -> float:
    parsed_lines = _cart_parse_lines(request)
    subtotal = sum(float(line["line_total"]) for line in parsed_lines)
    return round(float(subtotal), 2)


def _cart_totals(request: HttpRequest) -> dict:
    """
    Computes totals using MenuItem base prices + selected addon prices.
    Returns dict: subtotal, delivery_fee, total, count, lines[]
    """
    parsed_lines = _cart_parse_lines(request)

    subtotal = sum(float(line["line_total"]) for line in parsed_lines)
    count = sum(int(line["qty"]) for line in parsed_lines)
    fee = float(request.session.get("delivery_fee", 0) or 0)

    # coupon discount applies to subtotal only
    discount, coupon_info = _coupon_discount_for_request(request, subtotal)

    coupon = _get_coupon_from_session(request)

    if coupon and coupon.discount_type == DeliveryCoupon.DISCOUNT_FREE_DELIVERY:
        ok = coupon.grants_free_delivery(Decimal(str(subtotal)))
        if ok:
            fee = 0.0
            coupon_info["free_delivery"] = True
        else:
            coupon_info["free_delivery"] = False

    if count <= 0:
        fee = 0.0
        discount = 0.0
        subtotal = 0.0

        coupon = _get_coupon_from_session(request)
        if coupon:
            coupon_info = {
                "active": True,
                "code": coupon.code,
                "type": coupon.discount_type,
                "value": float(coupon.discount_value or 0),
                "min_subtotal": float(coupon.min_subtotal or 0),
                "free_delivery": False,
            }
            if coupon.discount_type == DeliveryCoupon.DISCOUNT_FREE_DELIVERY:
                coupon_info["free_delivery"] = False
        else:
            coupon_info = {"active": False}

    total = max(0.0, float(subtotal) - float(discount)) + float(fee)

    return {
        "subtotal": round(subtotal, 2),
        "delivery_fee": round(fee, 2),
        "coupon_discount": round(float(discount), 2),
        "coupon": coupon_info,
        "total": round(total, 2),
        "count": count,
        "lines": parsed_lines,
        "delivery_fee_waived": bool(coupon_info.get("free_delivery")),
    }

def delivery_location(request: HttpRequest) -> HttpResponse:
    promo = _active_promo()
    ctx = {
        "rest_lat": getattr(settings, "RESTAURANT_LAT", 0),
        "rest_lng": getattr(settings, "RESTAURANT_LNG", 0),
        "max_radius": getattr(settings, "DELIVERY_MAX_RADIUS_KM", 10.0),
        "promo_active": bool(promo),
        "promo_title": promo.title if promo else "",
        "promo_min_subtotal": float(promo.min_subtotal) if promo else 0.0,
        "promo_free_delivery": bool(promo.free_delivery) if promo else False,
        # Optional: only if you ever want to render/use it in the partial itself
        "GOOGLE_MAPS_API_KEY": getattr(settings, "GOOGLE_MAPS_API_KEY", ""),
    }

    return render(request, "delivery_location.html", ctx)


@require_POST
def delivery_calc(request: HttpRequest) -> JsonResponse:
    try:
        lat = float(request.POST.get("lat"))
        lng = float(request.POST.get("lng"))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "Invalid coordinates"}, status=400)

    rest_lat = float(getattr(settings, "RESTAURANT_LAT", 0))
    rest_lng = float(getattr(settings, "RESTAURANT_LNG", 0))

    distance_km = haversine_km(rest_lat, rest_lng, lat, lng)
    base_fee = float(delivery_fee_for_distance(distance_km))

    max_radius = float(getattr(settings, "DELIVERY_MAX_RADIUS_KM", 10.0))
    in_range = distance_km <= max_radius

    subtotal = _cart_subtotal(request)
    final_fee, promo_info = _apply_promo_delivery_fee(base_fee, subtotal)

    return JsonResponse(
        {
            "ok": True,
            "distance_km": round(distance_km, 2),
            "delivery_fee": round(float(final_fee), 2),
            "base_delivery_fee": round(float(base_fee), 2),
            "subtotal": round(float(subtotal), 2),
            "estimated_total": round(float(subtotal + float(final_fee)), 2),
            "in_range": in_range,
            "max_radius_km": max_radius,
            "promo": promo_info,
        }
    )


@require_POST
def delivery_set_location(request: HttpRequest) -> HttpResponse:
    """Save location + fee + address label to session."""
    try:
        lat = float(request.POST.get("lat"))
        lng = float(request.POST.get("lng"))
    except (TypeError, ValueError):
        return redirect(reverse("restaurant:menu") + "?open_location=1")

    address_label = (request.POST.get("address_label") or "").strip()

    rest_lat = float(getattr(settings, "RESTAURANT_LAT", 0))
    rest_lng = float(getattr(settings, "RESTAURANT_LNG", 0))

    distance_km = haversine_km(rest_lat, rest_lng, lat, lng)
    base_fee = float(delivery_fee_for_distance(distance_km))

    subtotal = _cart_subtotal(request)
    final_fee, promo_info = _apply_promo_delivery_fee(base_fee, subtotal)

    request.session["delivery_lat"] = lat
    request.session["delivery_lng"] = lng
    request.session["delivery_distance_km"] = round(distance_km, 2)
    request.session["delivery_fee"] = round(float(final_fee), 2)
    request.session["delivery_base_fee"] = round(float(base_fee), 2)
    request.session["delivery_promo"] = promo_info
    request.session["delivery_address_label"] = address_label
    request.session.modified = True

    return redirect(reverse("restaurant:menu") + "?delivery=1")


# -------------------------
# Cart endpoints (AJAX)
# -------------------------


@require_POST
def delivery_cart_add(request: HttpRequest) -> JsonResponse:
    """POST: item_id, qty(optional), selected_options[] (optional)"""
    try:
        item_id = int(request.POST.get("item_id"))
        qty = int(request.POST.get("qty", 1))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid payload"}, status=400)

    if qty <= 0:
        qty = 1

    mi = get_object_or_404(MenuItem, id=item_id)
    if mi.status == MenuItem.STATUS_HIDDEN:
        return JsonResponse({"ok": False, "error": "Item not available"}, status=404)

    raw_selected_options = request.POST.getlist("selected_options")
    valid_option_ids, selected_options, errors = _validate_selected_addons_for_item(
        mi,
        raw_selected_options,
    )

    if errors:
        return JsonResponse(
            {
                "ok": False,
                "error": "Please fix addon selections.",
                "addon_errors": errors,
            },
            status=400,
        )

    cart = _cart_get(request.session)
    items = cart["items"]

    line_key = _build_cart_line_key(item_id, valid_option_ids)
    current_row = items.get(line_key) or {
        "item_id": item_id,
        "qty": 0,
        "selected_options": valid_option_ids,
    }

    current_qty = int(current_row.get("qty", 0))
    items[line_key] = {
        "item_id": item_id,
        "qty": current_qty + qty,
        "selected_options": valid_option_ids,
    }

    request.session["delivery_cart"] = cart
    request.session.modified = True

    totals = _cart_totals(request)
    return JsonResponse({"ok": True, "cart": totals})

@require_POST
def delivery_cart_update(request: HttpRequest) -> JsonResponse:
    """POST: cart_key, qty"""
    cart_key = (request.POST.get("cart_key") or "").strip()

    try:
        qty = int(request.POST.get("qty"))
    except Exception:
        return JsonResponse({"ok": False, "error": "Invalid payload"}, status=400)

    if not cart_key:
        return JsonResponse({"ok": False, "error": "Missing cart key"}, status=400)

    cart = _cart_get(request.session)
    items = cart["items"]

    if cart_key not in items:
        return JsonResponse({"ok": False, "error": "Cart line not found"}, status=404)

    if qty <= 0:
        del items[cart_key]
    else:
        row = items[cart_key]
        row["qty"] = qty
        items[cart_key] = row

    request.session["delivery_cart"] = cart
    request.session.modified = True

    totals = _cart_totals(request)
    return JsonResponse({"ok": True, "cart": totals})


@require_GET
def delivery_cart_summary(request: HttpRequest) -> JsonResponse:
    totals = _cart_totals(request)
    return JsonResponse({"ok": True, "cart": totals})


# -------------------------
# Nominatim helpers
# -------------------------


@require_GET
def nominatim_search(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip()
    if len(q) < 3:
        return JsonResponse({"ok": True, "results": []})

    params = {
        "q": q,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 6,
        "countrycodes": "fi",
    }

    url = "https://nominatim.openstreetmap.org/search?" + urlencode(params)

    try:
        req = Request(url, headers=_nominatim_headers())
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = [
            {
                "display_name": it.get("display_name", ""),
                "lat": it.get("lat"),
                "lon": it.get("lon"),
            }
            for it in data
        ]
        return JsonResponse({"ok": True, "results": results})
    except Exception:
        return JsonResponse({"ok": False, "results": []}, status=200)


@require_GET
def nominatim_reverse(request: HttpRequest) -> JsonResponse:
    lat = request.GET.get("lat")
    lon = request.GET.get("lon")
    if not lat or not lon:
        return JsonResponse({"ok": False, "label": ""})

    params = {"lat": lat, "lon": lon, "format": "jsonv2", "addressdetails": 1}
    url = "https://nominatim.openstreetmap.org/reverse?" + urlencode(params)

    try:
        req = Request(url, headers=_nominatim_headers())
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return JsonResponse({"ok": True, "label": data.get("display_name", "")})
    except Exception:
        return JsonResponse({"ok": False, "label": ""})


def _nominatim_headers() -> dict:
    ua = getattr(
        settings,
        "NOMINATIM_USER_AGENT",
        "RavintolaSinet/1.0 (contact: info@ravintola-sinet.fi)",
    )
    return {
        "User-Agent": ua,
        "Accept": "application/json",
        "Referer": getattr(settings, "SITE_URL", "http://127.0.0.1:8000/"),
    }


# -------------------------
# Promotions placeholders
# -------------------------


@login_required
def promotions_list(request: HttpRequest) -> HttpResponse:
    try:
        from .models import Promotion  # optional

        promos = Promotion.objects.all().order_by("-id")
    except Exception:
        promos = []

    try:
        return render(request, "admin/promotions.html", {"promos": promos})
    except Exception:
        html = "<h2>Promotions</h2><p>Promotions UI not created yet.</p>"
        html += "<p>Your URLs are working now ✅</p>"
        return HttpResponse(html)


@login_required
def add_promotion(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        messages.success(request, "Promotion saved (placeholder).")
        return redirect("restaurant:promotions_list")

    try:
        return render(request, "admin/promotion_add.html")
    except Exception:
        return HttpResponse("<h2>Add Promotion</h2><p>Template not created yet.</p>")


@login_required
def edit_promotion(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method == "POST":
        messages.success(request, "Promotion updated (placeholder).")
        return redirect("restaurant:promotions_list")

    try:
        return render(request, "admin/promotion_edit.html", {"pk": pk})
    except Exception:
        return HttpResponse(f"<h2>Edit Promotion</h2><p>ID: {pk}</p>")


@login_required
def delete_promotion(request: HttpRequest, pk: int) -> HttpResponse:
    if request.method == "POST":
        messages.success(request, "Promotion deleted (placeholder).")
        return redirect("restaurant:promotions_list")

    try:
        return render(request, "admin/promotion_delete.html", {"pk": pk})
    except Exception:
        return HttpResponse(f"<h2>Delete Promotion</h2><p>ID: {pk}</p>")


# -------------------------
# Delivery checkout + coupons + place order
# -------------------------


@ensure_csrf_cookie
def delivery_checkout(request: HttpRequest) -> HttpResponse:
    # Must have location
    if not request.session.get("delivery_lat") or not request.session.get(
        "delivery_lng"
    ):
        messages.error(request, _("Please set your delivery location first."))
        return redirect(reverse("restaurant:menu") + "?open_location=1")

    # If form was posted: save info to session then redirect
    if request.method == "POST":
        request.session["customer_name"] = (request.POST.get("name") or "").strip()
        request.session["customer_phone"] = (request.POST.get("phone") or "").strip()
        request.session["customer_note"] = (request.POST.get("note") or "").strip()
        request.session["customer_address_extra"] = (
            request.POST.get("address_extra") or ""
        ).strip()

        pm = (request.POST.get("payment_method") or "").strip()
        if pm in ["cash", "card"]:
            request.session["payment_method"] = pm

        request.session.modified = True
        return redirect("restaurant:delivery_checkout")

    # ✅ 1) Order-confirm modal logic FIRST
    placed = (request.GET.get("placed") or "").strip() == "1"
    order_obj = None

    order_id = (request.GET.get("order") or "").strip()
    if not order_id.isdigit():
        order_id = str(request.session.get("last_delivery_order_id") or "")

    if placed and order_id.isdigit():
        order_obj = (
            DeliveryOrder.objects.prefetch_related("items__addon_snapshots")
            .filter(id=int(order_id))
            .first()
        )

    # ✅ Security: only show modal for this session's latest order
    if order_obj and int(order_id) != int(
        request.session.get("last_delivery_order_id") or 0
    ):
        order_obj = None

    # ✅ 2) Then compute cart
    cart = _cart_totals(request)

    # ✅ 3) If cart empty, allow ONLY when order modal exists
    if int(cart.get("count") or 0) <= 0 and not order_obj:
        messages.error(request, _("Your cart is empty."))
        return redirect(reverse("restaurant:menu"))

    # Payment label
    pm = (request.session.get("payment_method") or "cash").strip()
    if pm not in ["cash", "card"]:
        pm = "cash"
    pay_method_label = (
        _("Cash on Delivery") if pm == "cash" else _("Card on Delivery (POS machine)")
    )

    ctx = {
        "address_label": request.session.get("delivery_address_label", ""),
        "distance_km": request.session.get("delivery_distance_km", 0),
        "delivery_fee": request.session.get("delivery_fee", 0),
        "promo": request.session.get("delivery_promo") or {"active": False},
        "cart": cart,
        "name": request.session.get("customer_name", ""),
        "phone": request.session.get("customer_phone", ""),
        "note": request.session.get("customer_note", ""),
        "address_extra": request.session.get("customer_address_extra", ""),
        "pay_method": pay_method_label,
        "coupon_code": request.session.get("delivery_coupon_code", ""),
        "show_order_modal": bool(order_obj),
        "order_obj": order_obj,
        "loyalty": _loyalty_ui_context(request.user),
    }

    # ✅ show confirmation only once
    if order_obj:
        request.session.pop("last_delivery_order_id", None)
        request.session.modified = True

    return render(request, "delivery_checkout.html", ctx)


@require_POST
def delivery_apply_coupon(request: HttpRequest):
    """
    Apply coupon:
    - If AJAX: return JSON (no redirect)
    - Else: redirect back to previous page (fallback)
    """
    code = (request.POST.get("coupon_code") or "").strip()

    # helper for fallback redirect
    def _back():
        return redirect(request.META.get("HTTP_REFERER") or reverse("restaurant:menu"))

    if not code:
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": False, "error": "Please enter a coupon code."}, status=400
            )
        messages.error(request, "Please enter a coupon code.")
        return _back()

    coupon = DeliveryCoupon.objects.filter(code__iexact=code).first()
    if not coupon or not coupon.is_current():
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse(
                {"ok": False, "error": "Invalid or expired coupon."}, status=400
            )
        messages.error(request, "Invalid or expired coupon.")
        return _back()

    # check min subtotal against current subtotal (before coupon)
    cart = _cart_totals(request)
    subtotal = Decimal(str(cart.get("subtotal") or 0))
    if subtotal < Decimal(str(coupon.min_subtotal or 0)):
        msg = f"Coupon requires minimum subtotal € {coupon.min_subtotal}."
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return _back()

    request.session["delivery_coupon_code"] = coupon.code
    request.session.modified = True

    # return updated totals
    updated = _cart_totals(request)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "cart": updated})

    messages.success(request, f"Coupon {coupon.code} applied.")
    return _back()


@require_POST
def delivery_remove_coupon(request: HttpRequest):
    """
    Remove coupon:
    - If AJAX: return JSON (no redirect)
    - Else: redirect back to previous page (fallback)
    """
    request.session.pop("delivery_coupon_code", None)
    request.session.modified = True

    updated = _cart_totals(request)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "cart": updated})

    messages.success(request, "Coupon removed.")
    return redirect(request.META.get("HTTP_REFERER") or reverse("restaurant:menu"))



def _normalize_fi_phone(raw: str) -> str:
    """
    Validate Finnish phone number.
    Allowed formats ONLY:
      - +358XXXXXXXX
      - 358XXXXXXXX
    Returns cleaned string (spaces/dashes removed) or "" if invalid.
    """
    s = (raw or "").strip()
    if not s:
        return ""

    # remove spaces/dashes/parentheses
    s = re.sub(r"[()\s\-]", "", s)

    # must be digits with optional leading +
    if not re.fullmatch(r"\+?\d+", s):
        return ""

    # +358........
    if s.startswith("+358"):
        rest = s[4:]
        if 6 <= len(rest) <= 12:
            return s

    # 358........ (no +)
    if s.startswith("358"):
        rest = s[3:]
        if 6 <= len(rest) <= 12:
            return s

    return ""


def _is_ajax(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


@require_POST
def delivery_place_order(request: HttpRequest) -> HttpResponse:
    # must have location
    if not request.session.get("delivery_lat") or not request.session.get(
        "delivery_lng"
    ):
        messages.error(request, "Please set your delivery location first.")
        return redirect(reverse("restaurant:menu") + "?open_location=1")

    # must have cart items
    cart = _cart_totals(request)
    lines = cart.get("lines") or []
    if not lines:
        messages.error(request, "Your cart is empty.")
        return redirect(reverse("restaurant:menu") + "?delivery=1")

    # ✅ FIX: move this OUTSIDE the if-block
    name = (request.POST.get("name") or "").strip() or (
        request.session.get("customer_name") or ""
    ).strip()
    phone = (request.POST.get("phone") or "").strip() or (
        request.session.get("customer_phone") or ""
    ).strip()
    note = (request.POST.get("note") or "").strip() or (
        request.session.get("customer_note") or ""
    ).strip()
    extra = (request.POST.get("address_extra") or "").strip() or (
        request.session.get("customer_address_extra") or ""
    ).strip()
    payment_method = (request.POST.get("payment_method") or "cash").strip()

    if payment_method not in ["cash", "card"]:
        payment_method = "cash"

    if not name or not phone:
        messages.error(request, _("Please enter your name and phone number."))
        return redirect("restaurant:delivery_checkout")

    normalized_phone = _normalize_fi_phone(phone)
    if not normalized_phone:
        messages.error(
            request,
            _("Please use a valid Finland number to order (start with +358 or 358).")
        )
        return redirect("restaurant:delivery_checkout")

    # use normalized phone everywhere from now on
    phone = normalized_phone
    request.session["customer_phone"] = phone
    request.session.modified = True

    lat = float(request.session["delivery_lat"])
    lng = float(request.session["delivery_lng"])
    distance_km = float(request.session.get("delivery_distance_km") or 0)
    address_label = (request.session.get("delivery_address_label") or "").strip()

    promo = request.session.get("delivery_promo") or {"active": False}

    subtotal = Decimal(str(cart.get("subtotal") or 0))
    fee = Decimal(str(cart.get("delivery_fee") or 0))
    total = Decimal(str(cart.get("total") or (float(subtotal) + float(fee))))

    # coupon snapshot
    coupon = _get_coupon_from_session(request)
    coupon_code = coupon.code if coupon else ""
    coupon_discount = Decimal("0")
    if coupon:
        coupon_discount = coupon.compute_discount(subtotal)

    # create order
    order = DeliveryOrder.objects.create(
        user=request.user if request.user.is_authenticated else None,
        customer_name=name,
        customer_phone=phone,
        customer_note=note,
        address_label=address_label,
        address_extra=extra,
        lat=lat,
        lng=lng,
        distance_km=distance_km,
        subtotal=subtotal,
        delivery_fee=fee,
        total=total,
        promo_title=str(promo.get("title") or ""),
        promo_free_delivery=bool(promo.get("free_delivery") or False),
        promo_min_subtotal=Decimal(str(promo.get("min_subtotal") or 0)),
        coupon_code=coupon_code,
        coupon_discount=coupon_discount,
        payment_method=payment_method,
    )
    # snapshot items + addon snapshots
    ids = [int(x["id"]) for x in lines if str(x.get("id", "")).isdigit()]
    menu_map = {
        m.id: m
        for m in MenuItem.objects.filter(id__in=ids).exclude(
            status=MenuItem.STATUS_HIDDEN
        )
    }

    created_order_items = []

    for line in lines:
        mid_raw = line.get("id")
        if not str(mid_raw).isdigit():
            continue
        mid = int(mid_raw)

        qty = int(line.get("qty") or 0)
        if qty <= 0:
            continue

        unit_price = Decimal(str(line.get("unit_price") or 0))
        addons_total = Decimal(str(line.get("addons_total") or 0))
        mi = menu_map.get(mid)

        order_item = DeliveryOrderItem.objects.create(
            order=order,
            menu_item=mi if mi else None,
            name=(mi.name if mi else str(line.get("name") or f"Item {mid}")),
            unit_price=unit_price,
            addons_total=addons_total,
            qty=qty,
        )
        created_order_items.append((order_item, line))

    addon_snapshot_bulk = []
    for order_item, line in created_order_items:
        for addon in (line.get("addons") or []):
            addon_snapshot_bulk.append(
                DeliveryOrderItemAddon(
                    order_item=order_item,
                    group_name=str(addon.get("group_name") or ""),
                    option_name=str(addon.get("option_name") or ""),
                    option_price=Decimal(str(addon.get("price") or 0)),
                )
            )

    if addon_snapshot_bulk:
        DeliveryOrderItemAddon.objects.bulk_create(addon_snapshot_bulk)

    # increment coupon usage (if any) — only if order successfully created
    if coupon:
        DeliveryCoupon.objects.filter(pk=coupon.pk).update(
            used_count=F("used_count") + 1
        )

    # clear cart + coupon (keep location optional)
    request.session["delivery_cart"] = {"items": {}}
    request.session.pop("delivery_coupon_code", None)

    # ✅ store last order id so we can safely show it once
    request.session["last_delivery_order_id"] = order.id
    request.session.modified = True

    # ✅ optional: bind order to current session for security
    request.session.setdefault(
        "delivery_session_key", request.session.session_key or ""
    )
    request.session.modified = True

    # --- Telegram notify with inline buttons (safe: never breaks order) ---
    try:
        order = DeliveryOrder.objects.prefetch_related("items__addon_snapshots").get(id=order.id)

        msg = build_delivery_order_message(order)
        keyboard = build_delivery_status_keyboard(order.id, order.status)

        tg_result = send_telegram_message_full(
            text=msg,
            reply_markup=keyboard,
        )

        tg_message = (tg_result.get("result") or {})
        tg_chat = (tg_message.get("chat") or {})
        tg_message_id = tg_message.get("message_id")
        tg_chat_id = tg_chat.get("id")

        updates = []
        if tg_chat_id is not None:
            order.telegram_chat_id = str(tg_chat_id)
            updates.append("telegram_chat_id")

        if tg_message_id is not None:
            order.telegram_message_id = int(tg_message_id)
            updates.append("telegram_message_id")

        order.telegram_last_status_sent = order.status
        updates.append("telegram_last_status_sent")

        if updates:
            order.save(update_fields=updates)

        try:
            from restaurant.models import TelegramLog
            TelegramLog.objects.create(
                ok=True,
                kind="delivery",
                chat_id=str(order.telegram_chat_id or ""),
                message_preview=f"delivery order #{order.id} sent with buttons",
                response_text=str(tg_result)[:1500],
            )
        except Exception:
            pass

    except Exception as e:
        try:
            from restaurant.models import TelegramLog
            TelegramLog.objects.create(
                ok=False,
                kind="delivery",
                chat_id=str(getattr(settings, "TELEGRAM_GROUP_CHAT_ID", "")),
                message_preview="delivery_place_order failed to build/send telegram",
                response_text=repr(e),
            )
        except Exception:
            pass

    # ✅ Redirect back to checkout and include the order id in URL (Option A)
    return redirect(
        reverse("restaurant:delivery_checkout") + f"?placed=1&order={order.id}"
    )


# -------------------------
# Admin: Delivery orders
# -------------------------


@require_POST
@login_required
def delivery_orders_bulk_update(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("order_ids")
    new_status = (request.POST.get("new_status") or "").strip()

    valid = {k for k, _ in DeliveryOrder.STATUS_CHOICES}
    if new_status not in valid:
        messages.error(request, "Please choose a valid status.")
        return redirect("restaurant:delivery_orders_list")

    ids_int = [int(x) for x in ids if str(x).isdigit()]
    if not ids_int:
        messages.error(request, "No orders selected.")
        return redirect("restaurant:delivery_orders_list")

    orders_to_update = list(DeliveryOrder.objects.filter(id__in=ids_int))
    updated = DeliveryOrder.objects.filter(id__in=ids_int).update(status=new_status)

    for order in orders_to_update:
        order.status = new_status
        send_delivery_status_email(order)

    # ✅ Loyalty: if bulk set to delivered, ensure coupons for affected users
    if new_status == DeliveryOrder.STATUS_DELIVERED:
        user_ids = (
            DeliveryOrder.objects.filter(id__in=ids_int)
            .exclude(user__isnull=True)
            .values_list("user_id", flat=True)
            .distinct()
        )
        for uid in user_ids:
            # load user via request.user model
            # safest: fetch the user object through DeliveryOrder relation
            u = (
                DeliveryOrder.objects.filter(user_id=uid)
                .values_list("user_id", flat=True)
                .first()
            )
            if u:
                # We need actual user instance:
                from django.contrib.auth import get_user_model

                User = get_user_model()
                user_obj = User.objects.filter(id=uid).first()
                if user_obj:
                    _ensure_loyalty_coupon_for_user(user_obj)

    messages.success(request, f"Updated {updated} order(s).")
    return redirect("restaurant:delivery_orders_list")

@require_POST
@login_required
def customer_mark_order_received(request: HttpRequest, order_id: int) -> HttpResponse:
    """
    Customer confirms the order is received.
    Only allowed if:
    - order belongs to logged-in user
    - status is out_for_delivery
    """
    o = get_object_or_404(DeliveryOrder, id=order_id, user=request.user)

    if o.status != DeliveryOrder.STATUS_OUT_FOR_DELIVERY:
        messages.error(request, _("This order cannot be marked as received right now."))
        return redirect("accounts:my_orders")  # ✅ change if your url name is different

    o.status = DeliveryOrder.STATUS_DELIVERED
    o.save(update_fields=["status"])
    send_delivery_status_email(o)

    # loyalty coupon logic stays consistent
    _ensure_loyalty_coupon_for_user(request.user)

    messages.success(request, _("Thanks! Your order is marked as delivered."))
    return redirect("accounts:my_orders")  # ✅ change if needed


@require_POST
@login_required
def delivery_orders_bulk_delete(request: HttpRequest) -> HttpResponse:
    ids = request.POST.getlist("order_ids")
    ids_int = [int(x) for x in ids if str(x).isdigit()]

    if not ids_int:
        messages.error(request, "No orders selected.")
        return redirect("restaurant:delivery_orders_list")

    # Delete order + items (items are CASCADE)
    deleted_count, _ = DeliveryOrder.objects.filter(id__in=ids_int).delete()
    messages.success(request, f"Deleted {deleted_count} record(s).")
    return redirect("restaurant:delivery_orders_list")


@login_required
def delivery_orders_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()

    qs = DeliveryOrder.objects.all().order_by("-created_at")

    if q:
        qs = qs.filter(
            Q(customer_name__icontains=q)
            | Q(customer_phone__icontains=q)
            | Q(address_label__icontains=q)
            | Q(address_extra__icontains=q)
            | Q(id__icontains=q)
        )

    if status:
        qs = qs.filter(status=status)

    return render(
        request,
        "admin/delivery_orders.html",
        {
            "orders": qs[:400],
            "q": q,
            "status": status,
            "status_choices": DeliveryOrder.STATUS_CHOICES,
        },
    )


@login_required
def delivery_order_detail_admin(request: HttpRequest, pk: int) -> HttpResponse:
    o = get_object_or_404(
        DeliveryOrder.objects.prefetch_related("items__addon_snapshots"),
        pk=pk,
    )
    return render(request, "admin/delivery_order_detail.html", {"o": o})

@login_required
def delivery_order_update_status(request: HttpRequest, pk: int) -> HttpResponse:
    o = get_object_or_404(DeliveryOrder, pk=pk)

    if request.method == "POST":
        new_status = (request.POST.get("status") or "").strip()
        valid = {k for k, _ in DeliveryOrder.STATUS_CHOICES}

        if new_status in valid:
            o.status = new_status
            o.save(update_fields=["status"])
            send_delivery_status_email(o)
            messages.success(request, "Order status updated.")

            # ✅ Loyalty: when order becomes delivered, grant coupon if eligible
            if new_status == DeliveryOrder.STATUS_DELIVERED and o.user:
                _ensure_loyalty_coupon_for_user(o.user)
        else:
            messages.error(request, "Invalid status.")

    return redirect("restaurant:delivery_order_detail_admin", pk=pk)


@login_required
def delivery_coupons_list(request: HttpRequest) -> HttpResponse:
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()  # active/inactive/all

    qs = DeliveryCoupon.objects.all().order_by("-created_at")

    if q:
        qs = qs.filter(code__icontains=q)

    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "inactive":
        qs = qs.filter(is_active=False)

    return render(
        request,
        "admin/delivery_coupons.html",
        {
            "coupons": qs[:500],
            "q": q,
            "status": status,
        },
    )


@login_required
def delivery_coupon_add(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = DeliveryCouponForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.code = obj.code.strip().upper()
            obj.save()
            messages.success(request, f"Coupon {obj.code} created.")
            return redirect("restaurant:delivery_coupons_list")
    else:
        form = DeliveryCouponForm()

    return render(
        request, "admin/delivery_coupon_form.html", {"form": form, "mode": "add"}
    )


@login_required
def delivery_coupon_edit(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(DeliveryCoupon, pk=pk)

    if request.method == "POST":
        form = DeliveryCouponForm(request.POST, instance=obj)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.code = obj.code.strip().upper()
            obj.save()
            messages.success(request, f"Coupon {obj.code} updated.")
            return redirect("restaurant:delivery_coupons_list")
    else:
        form = DeliveryCouponForm(instance=obj)

    return render(
        request,
        "admin/delivery_coupon_form.html",
        {"form": form, "mode": "edit", "obj": obj},
    )


@login_required
def delivery_coupon_delete(request: HttpRequest, pk: int) -> HttpResponse:
    obj = get_object_or_404(DeliveryCoupon, pk=pk)

    if request.method == "POST":
        code = obj.code
        obj.delete()
        messages.success(request, f"Coupon {code} deleted.")
        return redirect("restaurant:delivery_coupons_list")

    return render(request, "admin/delivery_coupon_delete.html", {"obj": obj})


@login_required
def hero_banners_list(request: HttpRequest) -> HttpResponse:
    banners = HeroBanner.objects.all()
    return render(request, "admin/hero_banners.html", {"banners": banners})


@login_required
def hero_banner_add(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = HeroBannerForm(request.POST, request.FILES)
        if form.is_valid():
            form.save()
            messages.success(request, "Hero banner added.")
            return redirect("restaurant:hero_banners_list")
    else:
        form = HeroBannerForm()

    return render(request, "admin/hero_banner_form.html", {"form": form})


@login_required
def hero_banner_edit(request: HttpRequest, pk: int) -> HttpResponse:
    banner = get_object_or_404(HeroBanner, pk=pk)

    if request.method == "POST":
        form = HeroBannerForm(request.POST, request.FILES, instance=banner)
        if form.is_valid():
            form.save()
            messages.success(request, "Hero banner updated.")
            return redirect("restaurant:hero_banners_list")
    else:
        form = HeroBannerForm(instance=banner)

    return render(
        request, "admin/hero_banner_form.html", {"form": form, "banner": banner}
    )


@login_required
def hero_banner_delete(request: HttpRequest, pk: int) -> HttpResponse:
    banner = get_object_or_404(HeroBanner, pk=pk)

    if request.method == "POST":
        banner.delete()
        messages.success(request, "Hero banner deleted.")
        return redirect("restaurant:hero_banners_list")

    return render(request, "admin/hero_banner_delete.html", {"banner": banner})


@login_required
def loyalty_settings(request):
    from .models import LoyaltyProgram
    from django.contrib import messages

    obj = LoyaltyProgram.objects.first()

    if request.method == "POST":
        target = int(request.POST.get("target_orders") or 10)
        percent = int(request.POST.get("reward_percent") or 30)
        is_active = bool(request.POST.get("is_active"))

        if not obj:
            obj = LoyaltyProgram.objects.create(
                target_orders=target,
                reward_percent=percent,
                is_active=is_active,
            )
        else:
            obj.target_orders = target
            obj.reward_percent = percent
            obj.is_active = is_active
            obj.save()

        messages.success(request, "Loyalty settings updated.")
        return redirect("restaurant:loyalty_settings")

    return render(request, "admin/loyalty_settings.html", {"obj": obj})





@login_required
def delivery_pricing_settings(request):
    obj = DeliveryPricing.objects.order_by("-updated_at").first()

    if request.method == "POST":
        # read inputs safely
        is_active = bool(request.POST.get("is_active"))

        def to_dec(name, default):
            v = (request.POST.get(name) or "").strip()
            try:
                return Decimal(v)
            except Exception:
                return Decimal(default)

        base_km = to_dec("base_km", "2.00")
        base_fee = to_dec("base_fee", "1.99")
        per_km_fee = to_dec("per_km_fee", "0.99")
        max_fee = to_dec("max_fee", "8.99")

        if not obj:
            obj = DeliveryPricing.objects.create(
                is_active=is_active,
                base_km=base_km,
                base_fee=base_fee,
                per_km_fee=per_km_fee,
                max_fee=max_fee,
            )
        else:
            obj.is_active = is_active
            obj.base_km = base_km
            obj.base_fee = base_fee
            obj.per_km_fee = per_km_fee
            obj.max_fee = max_fee
            obj.save()

        messages.success(request, "Delivery pricing updated.")
        return redirect("restaurant:delivery_pricing_settings")

    return render(request, "admin/delivery_pricing_settings.html", {"obj": obj})


def reviews_page(request):
    # --------------------------
    # 1️⃣ Handle POST (SAVE REVIEW)
    # --------------------------
    if request.method == "POST":
        form = ReviewForm(request.POST)

        if form.is_valid():
            form.save()
            messages.success(request, _("Thank you! Your review has been submitted."))
            return redirect(reverse("restaurant:reviews"))
        else:
            messages.error(request, _("Please fix the errors below."))
    else:
        form = ReviewForm()

    # --------------------------
    # 2️⃣ Fetch updated reviews
    # --------------------------
    reviews_qs = Review.objects.all().order_by("-rating", "-id")

    total_reviews = reviews_qs.count()
    average_rating = reviews_qs.aggregate(avg=Avg("rating"))["avg"] or 0
    star_percentage = (average_rating / 5) * 100 if average_rating else 0

    # Rating distribution 5 → 1
    distribution = []
    for i in range(5, 0, -1):
        count = reviews_qs.filter(rating=i).count()
        percent = (count / total_reviews * 100) if total_reviews else 0
        distribution.append(
            {
                "stars": i,
                "count": count,
                "percent": percent,
            }
        )

    # Pagination
    reviews_list = list(reviews_qs)
    for review in reviews_list:
        review.star_width = (review.rating / 5) * 100

    paginator = Paginator(reviews_list, 9)
    page_number = request.GET.get("page")
    reviews = paginator.get_page(page_number)

    context = {
        "reviews": reviews,
        "total_reviews": total_reviews,
        "average_rating": round(average_rating, 1),
        "star_percentage": star_percentage,
        "distribution": distribution,
        "form": form,
    }

    return render(request, "reviews.html", context)

def delivery_location_partial(request: HttpRequest) -> HttpResponse:
    """
    Returns ONLY the location picker UI for the menu modal.
    Uses same context as delivery_location, but a partial template.
    """
    promo = _active_promo()
    ctx = {
        "rest_lat": getattr(settings, "RESTAURANT_LAT", 0),
        "rest_lng": getattr(settings, "RESTAURANT_LNG", 0),
        "max_radius": getattr(settings, "DELIVERY_MAX_RADIUS_KM", 10.0),
        "promo_active": bool(promo),
        "promo_title": promo.title if promo else "",
        "promo_min_subtotal": float(promo.min_subtotal) if promo else 0.0,
        "promo_free_delivery": bool(promo.free_delivery) if promo else False,
    }
    return render(request, "partials/delivery_location_modal.html", ctx)



@csrf_exempt
@require_POST
def telegram_webhook(request: HttpRequest) -> JsonResponse:
    """
    Telegram webhook endpoint.
    Handles inline button callback queries for delivery order status updates.
    """

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid json"}, status=400)

    callback_query = payload.get("callback_query") or {}
    if not callback_query:
        return JsonResponse({"ok": True})

    callback_id = str(callback_query.get("id") or "").strip()
    from_user = callback_query.get("from") or {}
    tg_user_id = from_user.get("id")
    tg_name = (
        from_user.get("username")
        or " ".join(
            x for x in [
                str(from_user.get("first_name") or "").strip(),
                str(from_user.get("last_name") or "").strip(),
            ] if x
        )
        or str(tg_user_id or "")
    )

    if not telegram_user_is_allowed(tg_user_id):
        try:
            answer_callback_query(
                callback_id,
                "You are not allowed to manage orders.",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    data = str(callback_query.get("data") or "").strip()
    parts = data.split(":")

    if len(parts) != 3 or parts[0] != "do":
        try:
            answer_callback_query(
                callback_id,
                "Invalid action.",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    _, raw_order_id, target_status = parts

    if not str(raw_order_id).isdigit():
        try:
            answer_callback_query(
                callback_id,
                "Invalid order id.",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    valid_statuses = {k for k, _ in DeliveryOrder.STATUS_CHOICES}
    if target_status not in valid_statuses:
        try:
            answer_callback_query(
                callback_id,
                "Invalid status.",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    order = (
        DeliveryOrder.objects
        .prefetch_related("items__addon_snapshots")
        .filter(id=int(raw_order_id))
        .first()
    )

    if not order:
        try:
            answer_callback_query(
                callback_id,
                "Order not found.",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    current_status = str(order.status or "").strip()

    if current_status == target_status:
        try:
            answer_callback_query(
                callback_id,
                f"Order already {delivery_status_label(current_status)}.",
                show_alert=False,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    if not _telegram_status_change_is_valid(current_status, target_status):
        try:
            answer_callback_query(
                callback_id,
                f"Invalid transition: {delivery_status_label(current_status)} → {delivery_status_label(target_status)}",
                show_alert=True,
            )
        except Exception:
            pass
        return JsonResponse({"ok": True})

    order.status = target_status
    order.telegram_last_action_by = str(tg_name)[:120]
    order.telegram_last_action_at = timezone.now()
    order.telegram_last_status_sent = target_status
    order.save(
        update_fields=[
            "status",
            "telegram_last_action_by",
            "telegram_last_action_at",
            "telegram_last_status_sent",
        ]
    )
    send_delivery_status_email(order)

    if target_status == DeliveryOrder.STATUS_DELIVERED and order.user:
        try:
            _ensure_loyalty_coupon_for_user(order.user)
        except Exception:
            pass

    message_text = build_delivery_order_message(order)
    keyboard = build_delivery_status_keyboard(order.id, order.status)

    chat_id = str(order.telegram_chat_id or "").strip()
    message_id = order.telegram_message_id

    try:
        if chat_id and message_id:
            edit_telegram_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message_text,
                reply_markup=keyboard,
            )
    except Exception:
        pass

    try:
        answer_callback_query(
            callback_id,
            f"Order #{order.id} updated to {delivery_status_label(order.status)}",
            show_alert=False,
        )
    except Exception:
        pass

    try:
        TelegramLog.objects.create(
            ok=True,
            kind="delivery_status_telegram",
            chat_id=chat_id,
            message_preview=f"Order #{order.id} -> {order.status} by {tg_name}",
            response_text="telegram callback handled",
        )
    except Exception:
        pass

    return JsonResponse({"ok": True})



@login_required
@require_GET
def customer_orders_status_api(request: HttpRequest) -> JsonResponse:
    orders = (
        DeliveryOrder.objects
        .filter(user=request.user)
        .order_by("-created_at")
        .values("id", "status")
    )

    status_map = dict(DeliveryOrder.STATUS_CHOICES)

    payload = [
        {
            "id": row["id"],
            "status": row["status"],
            "status_display": status_map.get(row["status"], row["status"]),
        }
        for row in orders
    ]

    return JsonResponse({
        "ok": True,
        "orders": payload,
    })
