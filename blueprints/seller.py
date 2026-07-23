import csv
import io
from flask import (Blueprint, render_template, redirect, url_for,
                   request, session, flash, current_app, jsonify, Response)
from datetime import datetime, timezone, timedelta
from utils.supabase_client import (db_select, db_insert, db_update,
                                   db_delete, storage_upload,
                                   escrow_mark_delivered, WalletOperationError)
from utils.decorators import login_required, seller_required, verified_required
from utils.helpers import (make_slug, sanitize_html, allowed_image, allowed_file,
                           safe_filename, calc_platform_fee, log_audit, generate_reference)
from utils.email import send_listing_status, send_sale_notification

seller_bp = Blueprint("seller", __name__)

def _seller_id():
    return session.get("user_id")


# ── Become Seller (redirect) ──────────────────────────────────

@seller_bp.route("/become-seller", methods=["GET"])
@login_required
def become_seller():
    if session.get("role") in ("seller", "admin"):
        return redirect(url_for("seller.dashboard"))
    return render_template("seller/become_seller.html")


# ── Seller Dashboard ──────────────────────────────────────────

@seller_bp.route("/dashboard")
@seller_required
def dashboard():
    sid = _seller_id()

    # Quick stats
    listings   = db_select("listings", "id,title,slug,status,sales_count,views,rating,review_count",
                           filters={"seller_id": sid})
    active     = [l for l in listings if l["status"] == "active"]
    pending_ap = [l for l in listings if l["status"] == "pending"]

    # NOTE (schema TODO): The `listings.status` CHECK constraint only allows
    # 'pending' | 'active' | 'paused' | 'rejected' | 'deleted'. There is no
    # true "draft" state (an unsubmitted, unpublished work-in-progress).
    # To support real draft products, add a boolean column, e.g.:
    #     ALTER TABLE public.listings ADD COLUMN is_draft BOOLEAN DEFAULT FALSE;
    # and treat draft creation as an insert with is_draft=true, status left
    # NULL/pending until the seller explicitly submits for review. Until that
    # migration exists, draft_count is always 0 and the Drafts tab is a
    # non-functional placeholder in the UI (by design, per instructions not
    # to implement schema-dependent features without the migration).
    draft_count = 0

    orders_all = db_select("orders", "id,status,total,seller_earnings,created_at,buyer_id,order_number",
                           filters={"seller_id": sid}, order="-created_at")
    pending_orders = [o for o in orders_all if o["status"] in ("pending", "processing")]
    completed      = [o for o in orders_all if o["status"] == "completed"]

    total_revenue = sum(float(o["total"]) for o in completed)
    total_sales   = len(completed)
    total_views   = sum(int(l.get("views") or 0) for l in listings)

    # Pending balance: earnings on orders not yet completed (i.e. not yet
    # credited to the wallet by deliver_order()). This is fully derivable
    # from existing columns — no schema change needed.
    pending_balance = sum(float(o.get("seller_earnings") or 0) for o in pending_orders)

    # Average rating across the seller's listings (weighted by review count).
    total_reviews_wt = sum(int(l.get("review_count") or 0) for l in listings)
    if total_reviews_wt > 0:
        avg_rating = sum(float(l.get("rating") or 0) * int(l.get("review_count") or 0)
                         for l in listings) / total_reviews_wt
    else:
        avg_rating = 0.0

    conversion_rate = round((total_sales / total_views * 100), 2) if total_views > 0 else 0.0

    # Total downloads across all of this seller's sold order items.
    seller_order_ids = [o["id"] for o in orders_all]
    total_downloads = 0
    if seller_order_ids:
        # db_select doesn't support "IN" filtering, so aggregate per-order.
        for oid in seller_order_ids:
            items = db_select("order_items", "download_count", filters={"order_id": oid})
            total_downloads += sum(int(i.get("download_count") or 0) for i in items)

    # Monthly revenue (last 6 months)
    monthly = {}
    for o in completed:
        dt = o.get("created_at", "")[:7]   # YYYY-MM
        if dt:
            monthly[dt] = monthly.get(dt, 0) + float(o["total"])
    monthly_labels = sorted(monthly)[-6:]
    monthly_values = [monthly.get(m, 0) for m in monthly_labels]

    # Recent orders
    recent_orders = orders_all[:6]

    # Recent sales (completed only)
    recent_sales = completed[:6]

    # Recent reviews (across all of the seller's listings)
    recent_reviews = db_select("reviews", "*", filters={"seller_id": sid},
                               order="-created_at", limit=6)
    for r in recent_reviews:
        buyer = db_select("users", "id,username", filters={"id": r["buyer_id"]}, single=True)
        r["buyer"] = buyer

    # Recent messages (conversations involving this seller)
    convos_1 = db_select("conversations", "*", filters={"participant_1": sid}, order="-last_message_at")
    convos_2 = db_select("conversations", "*", filters={"participant_2": sid}, order="-last_message_at")
    recent_messages = (convos_1 + convos_2)
    recent_messages.sort(key=lambda c: c.get("last_message_at") or "", reverse=True)
    recent_messages = recent_messages[:6]
    for c in recent_messages:
        other_id = c["participant_2"] if c["participant_1"] == sid else c["participant_1"]
        other    = db_select("users", "id,username", filters={"id": other_id}, single=True)
        c["other_user"] = other

    # Recent withdrawals
    recent_withdrawals = db_select("wallet_transactions", "*",
                                   filters={"user_id": sid, "type": "withdrawal"},
                                   order="-created_at", limit=6)

    # Recent notifications
    recent_notifications = db_select("notifications", "*", filters={"user_id": sid},
                                     order="-created_at", limit=6)

    # Pending manual deliveries
    manual_pending = []
    for o in pending_orders:
        items = db_select("order_items", "*",
                          filters={"order_id": o["id"], "delivery_status": "pending"})
        if items:
            manual_pending.append({"order": o, "items": items})

    user    = db_select("users", "id,username,balance,is_verified", filters={"id": sid}, single=True)
    profile = db_select("user_profiles", "*", filters={"user_id": sid}, single=True)

    return render_template("seller/dashboard.html",
        user=user, profile=profile,
        total_listings=len(listings),
        active_listings=len(active),
        pending_approval=len(pending_ap),
        draft_count=draft_count,
        pending_orders=len(pending_orders),
        total_revenue=total_revenue,
        total_sales=total_sales,
        total_orders=len(orders_all),
        total_views=total_views,
        total_downloads=total_downloads,
        avg_rating=avg_rating,
        conversion_rate=conversion_rate,
        pending_balance=pending_balance,
        recent_orders=recent_orders,
        recent_sales=recent_sales,
        recent_reviews=recent_reviews,
        recent_messages=recent_messages,
        recent_withdrawals=recent_withdrawals,
        recent_notifications=recent_notifications,
        manual_pending=manual_pending,
        monthly_labels=monthly_labels,
        monthly_values=monthly_values,
    )


# ── Create Listing ────────────────────────────────────────────

@seller_bp.route("/create", methods=["GET", "POST"])
@seller_required
@verified_required
def create_listing():
    categories = db_select("categories", filters={"is_active": True}, order="sort_order")

    if request.method == "POST":
        sid   = _seller_id()
        title = request.form.get("title", "").strip()
        cat   = request.form.get("category_id", "")
        desc  = sanitize_html(request.form.get("description", ""), strip=False)
        s_desc = request.form.get("short_description", "").strip()[:500]
        price  = request.form.get("price", "0")
        comp_price = request.form.get("compare_price", "")
        license_t  = request.form.get("license_type", "personal")
        version    = request.form.get("version", "1.0").strip()[:50]
        demo_url   = request.form.get("demo_url", "").strip()[:500]
        docs_url   = request.form.get("documentation_url", "").strip()[:500]
        support    = request.form.get("support_included") == "on"
        sup_days   = request.form.get("support_duration_days", "")
        updates    = request.form.get("updates_included") == "on"
        delivery_t = request.form.get("delivery_type", "instant")
        tags_raw   = request.form.get("tags", "")
        formats    = request.form.getlist("file_format")

        # Validation
        if not title:
            flash("Product title is required.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)
        try:
            price = float(price)
            assert price >= 0
        except (ValueError, AssertionError):
            flash("Enter a valid price.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)

        slug = make_slug(title)
        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

        listing_data = {
            "seller_id":     sid,
            "category_id":   cat or None,
            "title":         title,
            "slug":          slug,
            "description":   desc,
            "short_description": s_desc,
            "price":         price,
            "compare_price": float(comp_price) if comp_price else None,
            "license_type":  license_t,
            "version":       version,
            "demo_url":      demo_url or None,
            "documentation_url": docs_url or None,
            "support_included":  support,
            "support_duration_days": int(sup_days) if sup_days else None,
            "updates_included": updates,
            "delivery_type": delivery_t,
            "tags":          tags,
            "file_format":   formats,
            "status":        "pending",
            "is_approved":   False,
        }

        listing = db_insert("listings", listing_data)
        if not listing:
            flash("Failed to create listing. Please try again.", "danger")
            return render_template("seller/create_listing.html", categories=categories, form=request.form)

        lid = listing["id"]

        # Handle product image uploads
        bucket = current_app.config["SUPABASE_BUCKET"]
        preview_urls = []
        for i, f in enumerate(request.files.getlist("images")):
            if f and f.filename and allowed_image(f.filename):
                ext  = f.filename.rsplit(".", 1)[-1].lower()
                path = f"listings/{lid}/img_{i}.{ext}"
                url  = storage_upload(bucket, path, f.read(), f"image/{ext}")
                if url:
                    preview_urls.append(url)
                    db_insert("listing_images", {
                        "listing_id": lid, "url": url,
                        "is_primary": i == 0, "sort_order": i,
                    })

        if preview_urls:
            db_update("listings", {"preview_images": preview_urls}, {"id": lid})

        # Handle digital file upload
        product_file = request.files.get("product_file")
        if product_file and product_file.filename and allowed_file(product_file.filename):
            ext  = product_file.filename.rsplit(".", 1)[-1].lower()
            fn   = safe_filename(product_file.filename)
            path = f"products/{lid}/v{version}_{fn}"
            url  = storage_upload(bucket, path, product_file.read(),
                                  "application/octet-stream")
            if url:
                file_bytes = product_file.seek(0, 2)
                db_insert("listing_files", {
                    "listing_id": lid,
                    "version":    version,
                    "filename":   fn,
                    "file_url":   path,   # Supabase Storage path for signed URLs
                })
                db_update("listings", {"download_url": path}, {"id": lid})

        log_audit(sid, "create_listing", resource_type="listing", resource_id=lid,
                  details={"title": title})
        flash("Listing submitted for review! We'll notify you once approved.", "success")
        return redirect(url_for("seller.inventory"))

    return render_template("seller/create_listing.html",
                           categories=categories, form={})


# ── Edit Listing ──────────────────────────────────────────────

@seller_bp.route("/edit/<listing_id>", methods=["GET", "POST"])
@seller_required
def edit_listing(listing_id):
    sid     = _seller_id()
    listing = db_select("listings", "*", filters={"id": listing_id, "seller_id": sid}, single=True)
    if not listing:
        flash("Listing not found.", "danger")
        return redirect(url_for("seller.inventory"))

    categories = db_select("categories", filters={"is_active": True}, order="sort_order")
    images     = db_select("listing_images", "*", filters={"listing_id": listing_id},
                           order="sort_order")

    if request.method == "POST":
        title   = request.form.get("title", "").strip()
        desc    = sanitize_html(request.form.get("description", ""), strip=False)
        s_desc  = request.form.get("short_description", "").strip()[:500]
        price   = request.form.get("price", "0")
        cat     = request.form.get("category_id", "")
        comp    = request.form.get("compare_price", "")
        license_t = request.form.get("license_type", "personal")
        version = request.form.get("version", "1.0").strip()
        demo_url = request.form.get("demo_url", "").strip()
        docs_url = request.form.get("documentation_url", "").strip()
        support  = request.form.get("support_included") == "on"
        sup_days = request.form.get("support_duration_days", "")
        updates  = request.form.get("updates_included") == "on"
        tags_raw = request.form.get("tags", "")
        formats  = request.form.getlist("file_format")

        if not title:
            flash("Title is required.", "danger")
            return render_template("seller/edit_listing.html",
                                   listing=listing, categories=categories, images=images)
        try:
            price = float(price)
        except ValueError:
            flash("Enter a valid price.", "danger")
            return render_template("seller/edit_listing.html",
                                   listing=listing, categories=categories, images=images)

        tags = [t.strip().lower() for t in tags_raw.split(",") if t.strip()]

        db_update("listings", {
            "title":        title,
            "category_id":  cat or None,
            "description":  desc,
            "short_description": s_desc,
            "price":        price,
            "compare_price": float(comp) if comp else None,
            "license_type": license_t,
            "version":      version,
            "demo_url":     demo_url or None,
            "documentation_url": docs_url or None,
            "support_included": support,
            "support_duration_days": int(sup_days) if sup_days else None,
            "updates_included": updates,
            "tags":         tags,
            "file_format":  formats,
            "status":       "pending",   # Re-submit for review on edits
            "is_approved":  False,
        }, {"id": listing_id, "seller_id": sid})

        # Handle new image uploads
        bucket = current_app.config["SUPABASE_BUCKET"]
        for i, f in enumerate(request.files.getlist("new_images")):
            if f and f.filename and allowed_image(f.filename):
                ext  = f.filename.rsplit(".", 1)[-1].lower()
                path = f"listings/{listing_id}/img_{i}_{int(datetime.now().timestamp())}.{ext}"
                url  = storage_upload(bucket, path, f.read(), f"image/{ext}")
                if url:
                    db_insert("listing_images", {
                        "listing_id": listing_id, "url": url, "sort_order": 99,
                    })

        # Handle new file upload
        new_file = request.files.get("product_file")
        if new_file and new_file.filename and allowed_file(new_file.filename):
            fn   = safe_filename(new_file.filename)
            path = f"products/{listing_id}/v{version}_{fn}"
            storage_upload(bucket, path, new_file.read(), "application/octet-stream")
            db_insert("listing_files", {
                "listing_id": listing_id, "version": version,
                "filename": fn, "file_url": path,
            })
            db_update("listings", {"download_url": path}, {"id": listing_id})

        log_audit(sid, "edit_listing", resource_type="listing", resource_id=listing_id)
        flash("Listing updated and resubmitted for review.", "success")
        return redirect(url_for("seller.inventory"))

    return render_template("seller/edit_listing.html",
                           listing=listing, categories=categories, images=images)


# ── Delete / Pause / Activate ─────────────────────────────────

@seller_bp.route("/delete/<listing_id>", methods=["POST"])
@seller_required
def delete_listing(listing_id):
    sid = _seller_id()
    db_update("listings", {
        "status": "deleted",
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }, {"id": listing_id, "seller_id": sid})
    log_audit(sid, "delete_listing", resource_type="listing", resource_id=listing_id)
    flash("Listing deleted.", "success")
    return redirect(url_for("seller.inventory"))


@seller_bp.route("/pause/<listing_id>", methods=["POST"])
@seller_required
def pause_listing(listing_id):
    sid = _seller_id()
    db_update("listings", {"status": "paused"}, {"id": listing_id, "seller_id": sid})
    flash("Listing paused.", "info")
    return redirect(url_for("seller.inventory"))


@seller_bp.route("/activate/<listing_id>", methods=["POST"])
@seller_required
def activate_listing(listing_id):
    sid     = _seller_id()
    listing = db_select("listings", "is_approved",
                        filters={"id": listing_id, "seller_id": sid}, single=True)
    if not listing or not listing.get("is_approved"):
        flash("Listing must be approved before activation.", "warning")
    else:
        db_update("listings", {"status": "active"}, {"id": listing_id, "seller_id": sid})
        flash("Listing is now active.", "success")
    return redirect(url_for("seller.inventory"))


@seller_bp.route("/duplicate/<listing_id>", methods=["POST"])
@seller_required
def duplicate_listing(listing_id):
    """Create a copy of an existing listing as a new pending draft-for-review."""
    sid     = _seller_id()
    listing = db_select("listings", "*", filters={"id": listing_id, "seller_id": sid}, single=True)
    if not listing:
        flash("Listing not found.", "danger")
        return redirect(url_for("seller.inventory"))

    new_title = f"{listing['title']} (Copy)"
    copy_data = {
        "seller_id":          sid,
        "category_id":        listing.get("category_id"),
        "title":              new_title,
        "slug":               make_slug(new_title),
        "description":        listing.get("description"),
        "short_description":  listing.get("short_description"),
        "price":              listing.get("price"),
        "compare_price":      listing.get("compare_price"),
        "license_type":       listing.get("license_type"),
        "version":            listing.get("version"),
        "file_format":        listing.get("file_format"),
        "demo_url":           listing.get("demo_url"),
        "documentation_url":  listing.get("documentation_url"),
        "support_included":   listing.get("support_included"),
        "support_duration_days": listing.get("support_duration_days"),
        "updates_included":   listing.get("updates_included"),
        "delivery_type":      listing.get("delivery_type"),
        "tags":               listing.get("tags"),
        "status":             "pending",
        "is_approved":        False,
    }
    new_listing = db_insert("listings", copy_data)
    if not new_listing:
        flash("Failed to duplicate listing.", "danger")
        return redirect(url_for("seller.inventory"))

    # Copy image references (same URLs — files themselves aren't re-uploaded)
    images = db_select("listing_images", "*", filters={"listing_id": listing_id})
    for img in images:
        db_insert("listing_images", {
            "listing_id": new_listing["id"],
            "url":        img["url"],
            "is_primary": img.get("is_primary", False),
            "sort_order": img.get("sort_order", 0),
        })

    log_audit(sid, "duplicate_listing", resource_type="listing",
              resource_id=new_listing["id"], details={"source": listing_id})
    flash(f'Duplicated as "{new_title}". Edit and resubmit for review.', "success")
    return redirect(url_for("seller.edit_listing", listing_id=new_listing["id"]))


@seller_bp.route("/bulk-action", methods=["POST"])
@seller_required
def bulk_action():
    """Apply pause / activate / delete to multiple listings at once.
    Reuses the exact same per-item logic as the single-item routes above —
    no new business rules introduced."""
    sid         = _seller_id()
    action      = request.form.get("action", "")
    listing_ids = request.form.getlist("listing_ids")

    if not listing_ids:
        flash("No products selected.", "warning")
        return redirect(url_for("seller.inventory"))

    count = 0
    for lid in listing_ids:
        if action == "pause":
            db_update("listings", {"status": "paused"}, {"id": lid, "seller_id": sid})
            count += 1
        elif action == "activate":
            listing = db_select("listings", "is_approved",
                                filters={"id": lid, "seller_id": sid}, single=True)
            if listing and listing.get("is_approved"):
                db_update("listings", {"status": "active"}, {"id": lid, "seller_id": sid})
                count += 1
        elif action == "delete":
            db_update("listings", {
                "status": "deleted",
                "deleted_at": datetime.now(timezone.utc).isoformat(),
            }, {"id": lid, "seller_id": sid})
            count += 1

    log_audit(sid, "bulk_listing_action", details={"action": action, "count": count})
    flash(f"{count} product(s) updated.", "success")
    return redirect(url_for("seller.inventory"))


# ── Inventory ─────────────────────────────────────────────────

@seller_bp.route("/inventory")
@seller_required
def inventory():
    sid    = _seller_id()
    status = request.args.get("status", "")
    search = request.args.get("q", "").strip().lower()
    page   = int(request.args.get("page", 1))

    filters = {"seller_id": sid}
    if status:
        filters["status"] = status

    listings = db_select("listings", "*", filters=filters, order="-created_at")

    if search:
        listings = [l for l in listings if search in (l.get("title") or "").lower()]

    per_page  = 20
    total     = len(listings)
    start     = (page - 1) * per_page
    paginated = listings[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("seller/inventory.html",
        listings=paginated, status=status,
        search=search, page=page, pages=pages, total=total)


# ── Orders (Incoming) ─────────────────────────────────────────

@seller_bp.route("/orders")
@seller_required
def orders():
    sid    = _seller_id()
    status = request.args.get("status", "")
    page   = int(request.args.get("page", 1))

    filters = {"seller_id": sid}
    if status:
        filters["status"] = status

    all_orders = db_select("orders", "*", filters=filters, order="-created_at")
    for o in all_orders:
        buyer = db_select("users", "id,username,email",
                          filters={"id": o["buyer_id"]}, single=True)
        o["buyer"] = buyer
        o["items"] = db_select("order_items", "*", filters={"order_id": o["id"]})

    per_page  = 20
    total     = len(all_orders)
    start     = (page - 1) * per_page
    paginated = all_orders[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("seller/orders.html",
        orders=paginated, status=status, page=page, pages=pages, total=total)


@seller_bp.route("/orders/export")
@seller_required
def export_orders():
    """Export the seller's orders as a CSV file. Pure read-only, no schema change."""
    sid    = _seller_id()
    status = request.args.get("status", "")
    filters = {"seller_id": sid}
    if status:
        filters["status"] = status

    all_orders = db_select("orders", "*", filters=filters, order="-created_at")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Order Number", "Status", "Subtotal", "Discount",
                     "Platform Fee", "Total", "Seller Earnings",
                     "Payment Method", "Created At"])
    for o in all_orders:
        writer.writerow([
            o.get("order_number", ""),
            o.get("status", ""),
            o.get("subtotal", 0),
            o.get("discount_amount", 0),
            o.get("platform_fee", 0),
            o.get("total", 0),
            o.get("seller_earnings", 0),
            o.get("payment_method", ""),
            o.get("created_at", ""),
        ])

    log_audit(sid, "export_orders", details={"count": len(all_orders)})
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mercx_orders.csv"},
    )


@seller_bp.route("/orders/<order_id>/deliver", methods=["POST"])
@seller_required
def deliver_order(order_id):
    sid    = _seller_id()
    order  = db_select("orders", "*", filters={"id": order_id, "seller_id": sid}, single=True)
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("seller.orders"))

    item_id = request.form.get("item_id", "")
    content = request.form.get("delivery_content", "").strip()

    if not content:
        flash("Delivery content cannot be empty.", "danger")
        return redirect(url_for("seller.orders"))

    db_update("order_items", {
        "delivered_content": content,
        "delivery_status":   "delivered",
        "delivered_at":      datetime.now(timezone.utc).isoformat(),
    }, {"id": item_id, "order_id": order_id})

    # Check if all items delivered. Guard on order status too: if this
    # order was already marked "completed" (e.g. deliver_order fired
    # twice for the last remaining item — double-click, retry, or two
    # concurrent requests), don't re-trigger escrow a second time.
    remaining = db_select("order_items", "id",
                          filters={"order_id": order_id, "delivery_status": "pending"})
    if not remaining and order.get("status") != "completed":
        db_update("orders", {"status": "completed"}, {"id": order_id})

        # ESCROW: delivery no longer pays the seller directly. It starts
        # the escrow's auto-release countdown; the seller is paid when
        # the buyer confirms receipt (dashboard.purchases) or when the
        # auto-release deadline passes, whichever comes first. This
        # replaces the old "credit on delivery" flow, which paid the
        # seller before the buyer had any chance to dispute.
        esc = db_select("escrow_transactions", "id,status", filters={"order_id": order_id}, single=True)
        if esc:
            try:
                escrow_mark_delivered(
                    esc["id"], sid,
                    auto_release_hours=current_app.config.get("ESCROW_AUTO_RELEASE_HOURS", 72),
                )
            except WalletOperationError as e:
                current_app.logger.error(f"escrow_mark_delivered failed for order {order_id}: {e}")
                flash("Delivery recorded, but starting the payout timer failed. Contact support.", "warning")
        else:
            current_app.logger.error(f"deliver_order: no escrow transaction found for order {order_id}")

        db_insert("notifications", {
            "user_id": order["buyer_id"], "type": "order_delivered",
            "title": "Your order is ready!", "icon": "package",
            "message": f"Order {order['order_number']} has been delivered. Please confirm receipt.",
            "link": "/dashboard/purchases",
        })

    log_audit(sid, "deliver_order", resource_type="order", resource_id=order_id)
    flash("Delivery sent successfully.", "success")
    return redirect(url_for("seller.orders"))


# ── Analytics ─────────────────────────────────────────────────

@seller_bp.route("/analytics")
@seller_required
def analytics():
    sid = _seller_id()

    orders_all = db_select("orders", "id,status,total,created_at",
                           filters={"seller_id": sid})
    completed  = [o for o in orders_all if o["status"] == "completed"]

    # Revenue / Sales / Orders by month (last 12 months)
    monthly_rev   = {}
    monthly_sales = {}
    monthly_ord   = {}
    for o in orders_all:
        m = (o.get("created_at") or "")[:7]
        if not m:
            continue
        monthly_ord[m] = monthly_ord.get(m, 0) + 1
        if o["status"] == "completed":
            monthly_rev[m]   = monthly_rev.get(m, 0) + float(o["total"])
            monthly_sales[m] = monthly_sales.get(m, 0) + 1

    months_12     = sorted(set(monthly_rev) | set(monthly_ord))[-12:]
    revenue_data  = [monthly_rev.get(m, 0) for m in months_12]
    sales_data    = [monthly_sales.get(m, 0) for m in months_12]
    orders_data   = [monthly_ord.get(m, 0) for m in months_12]

    # Top listings by sales
    top_listings = db_select(
        "listings", "id,title,sales_count,views,rating",
        filters={"seller_id": sid}, order="-sales_count", limit=10
    )

    # Conversion: views vs sales
    all_seller_listings = db_select("listings", "views,download_count",
                                     filters={"seller_id": sid})
    total_views = sum(int(l.get("views") or 0) for l in all_seller_listings)
    total_sales = len(completed)
    conversion  = round((total_sales / total_views * 100), 2) if total_views > 0 else 0

    # Total downloads (aggregate — see dashboard() for the same derivation)
    total_downloads = 0
    for o in orders_all:
        items = db_select("order_items", "download_count", filters={"order_id": o["id"]})
        total_downloads += sum(int(i.get("download_count") or 0) for i in items)

    # NOTE (schema TODO): "Visitors" (unique daily page-view counts) cannot
    # be charted over time with the current schema. `listings.views` is a
    # single running counter incremented on each page load — it has no
    # per-day granularity and doesn't dedupe by visitor. To support a real
    # Visitors-over-time chart, add an events table, e.g.:
    #     CREATE TABLE public.listing_view_events (
    #         id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    #         listing_id UUID REFERENCES public.listings(id),
    #         visitor_hash TEXT,           -- hashed IP/session for dedup
    #         viewed_at TIMESTAMPTZ DEFAULT NOW()
    #     );
    # and log a row on each listing view. Until that migration exists, the
    # Visitors chart intentionally shows an empty state rather than
    # fabricated data.
    has_visitor_data = False

    # Wallet
    user = db_select("users", "balance", filters={"id": sid}, single=True)

    return render_template("seller/analytics.html",
        monthly_labels=months_12,
        monthly_values=revenue_data,
        sales_labels=months_12,
        sales_values=sales_data,
        orders_labels=months_12,
        orders_values=orders_data,
        top_listings=top_listings,
        total_orders=len(orders_all),
        completed_orders=total_sales,
        total_views=total_views,
        total_downloads=total_downloads,
        conversion_rate=conversion,
        total_revenue=sum(revenue_data),
        balance=float((user or {}).get("balance", 0)),
        has_visitor_data=has_visitor_data,
    )


# ── Store Settings ────────────────────────────────────────────

@seller_bp.route("/settings", methods=["GET", "POST"])
@seller_required
def store_settings():
    sid  = _seller_id()
    prof = db_select("user_profiles", "*", filters={"user_id": sid}, single=True)

    if request.method == "POST":
        store_name = request.form.get("store_name", "").strip()[:255]
        store_desc = sanitize_html(request.form.get("store_description", ""), strip=False)
        website    = request.form.get("website", "").strip()[:255]
        twitter    = request.form.get("twitter", "").strip()[:100]
        github     = request.form.get("github", "").strip()[:100]

        from python_slugify import slugify as _slugify
        slug = _slugify(store_name)

        db_update("user_profiles", {
            "store_name":        store_name,
            "store_slug":        slug,
            "store_description": store_desc,
            "website":           website,
            "twitter":           twitter,
            "github":            github,
        }, {"user_id": sid})

        # Banner upload
        banner = request.files.get("banner")
        if banner and banner.filename and allowed_image(banner.filename):
            bucket = current_app.config["SUPABASE_BUCKET"]
            ext    = banner.filename.rsplit(".", 1)[-1].lower()
            path   = f"banners/{sid}.{ext}"
            url    = storage_upload(bucket, path, banner.read(), f"image/{ext}")
            if url:
                db_update("user_profiles", {"store_banner_url": url}, {"user_id": sid})

        flash("Store settings updated.", "success")
        return redirect(url_for("seller.store_settings"))

    return render_template("seller/store_settings.html", profile=prof)


# ── Reviews ───────────────────────────────────────────────────

@seller_bp.route("/reviews")
@seller_required
def reviews():
    sid    = _seller_id()
    rating_filter = request.args.get("rating", "")
    page   = int(request.args.get("page", 1))

    filters = {"seller_id": sid}
    if rating_filter:
        filters["rating"] = int(rating_filter)

    all_reviews = db_select("reviews", "*", filters=filters, order="-created_at")
    for r in all_reviews:
        buyer = db_select("users", "id,username", filters={"id": r["buyer_id"]}, single=True)
        bprof = db_select("user_profiles", "avatar_url", filters={"user_id": r["buyer_id"]}, single=True)
        listing = db_select("listings", "id,title,slug", filters={"id": r["listing_id"]}, single=True)
        r["buyer"]   = buyer
        r["avatar"]  = bprof.get("avatar_url") if bprof else None
        r["listing"] = listing

    # Rating breakdown across ALL of this seller's reviews (unfiltered)
    unfiltered = db_select("reviews", "rating", filters={"seller_id": sid})
    total_count = len(unfiltered)
    avg_rating  = (sum(r["rating"] for r in unfiltered) / total_count) if total_count else 0
    rating_counts = {i: sum(1 for r in unfiltered if r["rating"] == i) for i in range(1, 6)}

    per_page  = 15
    total     = len(all_reviews)
    start     = (page - 1) * per_page
    paginated = all_reviews[start: start + per_page]
    pages     = max(1, -(-total // per_page))

    return render_template("seller/reviews.html",
        reviews=paginated, rating_filter=rating_filter,
        avg_rating=avg_rating, total_count=total_count,
        rating_counts=rating_counts,
        page=page, pages=pages, total=total)


# ── Withdrawals ───────────────────────────────────────────────

@seller_bp.route("/withdrawals")
@seller_required
def withdrawals():
    sid  = _seller_id()
    user = db_select("users", "id,username,balance", filters={"id": sid}, single=True)

    all_tx = db_select("wallet_transactions", "*",
                       filters={"user_id": sid, "type": "withdrawal"},
                       order="-created_at")
    pending_tx   = [t for t in all_tx if t["status"] == "pending"]
    completed_tx = [t for t in all_tx if t["status"] == "completed"]
    cancelled_tx = [t for t in all_tx if t["status"] == "cancelled"]

    # Pending balance: earnings not yet released (see dashboard() for derivation)
    orders_pending = db_select("orders", "seller_earnings,status",
                               filters={"seller_id": sid})
    pending_balance = sum(
        float(o.get("seller_earnings") or 0)
        for o in orders_pending if o["status"] in ("pending", "processing")
    )

    total_withdrawn = sum(float(t["amount"]) for t in completed_tx)

    cfg = current_app.config
    return render_template("seller/withdrawals.html",
        user=user,
        available_balance=float((user or {}).get("balance", 0)),
        pending_balance=pending_balance,
        total_withdrawn=total_withdrawn,
        all_tx=all_tx, pending_tx=pending_tx,
        completed_tx=completed_tx, cancelled_tx=cancelled_tx,
        min_withdrawal=cfg.get("MIN_WITHDRAWAL", 10),
        max_withdrawal=cfg.get("MAX_WITHDRAWAL", 10000),
    )
