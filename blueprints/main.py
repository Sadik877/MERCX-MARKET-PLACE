from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app
from utils.supabase_client import db_select, db_insert, get_supabase

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    # Featured listings
    featured = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,review_count,sales_count,preview_images,category_id",
        filters={"status": "active", "is_approved": True, "is_featured": True},
        order="-created_at", limit=8
    )
    # Popular (most sales)
    popular = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,review_count,sales_count,preview_images,seller_id",
        filters={"status": "active", "is_approved": True},
        order="-sales_count", limit=12
    )
    # New arrivals
    newest = db_select(
        "listings",
        "id,title,slug,price,compare_price,rating,review_count,preview_images",
        filters={"status": "active", "is_approved": True},
        order="-created_at", limit=8
    )
    # All categories
    categories = db_select("categories", filters={"is_active": True}, order="sort_order")

    # Stats
    stats = {
        "listings": db_select("listings", "id", filters={"status": "active", "is_approved": True}),
        "sellers":  db_select("user_profiles", "user_id", filters={"seller_verified": True}),
        "users":    db_select("users", "id"),
    }

    return render_template("index.html",
        featured=featured, popular=popular, newest=newest,
        categories=categories,
        total_listings=len(stats["listings"]),
        total_sellers=len(stats["sellers"]),
        total_users=len(stats["users"]),
    )


@main_bp.route("/search")
def search():
    q          = request.args.get("q", "").strip()
    category   = request.args.get("category", "")
    sort       = request.args.get("sort", "relevance")
    min_price  = request.args.get("min_price", "")
    max_price  = request.args.get("max_price", "")
    page       = int(request.args.get("page", 1))
    per_page   = 20

    results = []
    if q or category:
        listing_cols = ("id,title,slug,price,compare_price,rating,review_count,"
                         "sales_count,preview_images,category_id,seller_id,created_at")

        all_listings = None
        if q:
            # BUG-017 (partial): push the text match to Postgres via
            # .text_search() instead of pulling every active listing into
            # Python and running a substring check on the title. NOTE this
            # queries to_tsvector('english', title) on the fly — it does NOT
            # hit schema.sql's idx_listings_search GIN index, because that
            # index is built on a concatenated title+short_description+
            # description expression, and Supabase-py's text_search() only
            # accepts a real column name, not an arbitrary expression. To
            # actually use that index, a generated/stored tsvector column
            # mirroring the index expression would need to be added via
            # migration and queried instead — left as a follow-up (see
            # BUG_INVENTORY.md / FIX_ROADMAP.md) since it needs a live DB to
            # verify safely. This change still helps today: it moves the
            # filtering off the app server and gets real word-based/stemmed
            # matching instead of a naive substring check. Falls back to the
            # original fetch-then-filter-in-Python behavior on any error so
            # this can only make search faster/better when it succeeds, never
            # break it.
            try:
                sb_query = (get_supabase().table("listings")
                            .select(listing_cols)
                            .eq("status", "active")
                            .eq("is_approved", True)
                            .text_search("title", q, options={"config": "english", "type": "websearch"}))
                if category:
                    sb_query = sb_query.eq("category_id", category)
                sb_query = sb_query.order("sales_count", desc=True)
                all_listings = sb_query.execute().data or []
            except Exception as e:
                current_app.logger.warning(
                    f"main.search: DB-side text_search failed, falling back to "
                    f"Python filtering — {e}")
                all_listings = None

        if all_listings is None:
            all_listings = db_select(
                "listings", listing_cols,
                filters={"status": "active", "is_approved": True},
                order="-sales_count"
            )
            # Filter in Python (fallback path — see try/except above)
            filtered = []
            for listing in all_listings:
                if q and q.lower() not in (listing.get("title") or "").lower():
                    continue
                if category and listing.get("category_id") != category:
                    continue
                filtered.append(listing)
            all_listings = filtered

        for listing in all_listings:
            if min_price:
                try:
                    if float(listing["price"]) < float(min_price):
                        continue
                except (ValueError, TypeError):
                    pass
            if max_price:
                try:
                    if float(listing["price"]) > float(max_price):
                        continue
                except (ValueError, TypeError):
                    pass
            results.append(listing)

        # Sort
        if sort == "price_asc":
            results.sort(key=lambda x: float(x.get("price") or 0))
        elif sort == "price_desc":
            results.sort(key=lambda x: float(x.get("price") or 0), reverse=True)
        elif sort == "newest":
            results.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        elif sort == "popular":
            results.sort(key=lambda x: int(x.get("sales_count") or 0), reverse=True)
        elif sort == "rating":
            results.sort(key=lambda x: float(x.get("rating") or 0), reverse=True)

    categories = db_select("categories", filters={"is_active": True}, order="sort_order")
    total      = len(results)
    start      = (page - 1) * per_page
    paginated  = results[start: start + per_page]
    pages      = max(1, -(-total // per_page))

    return render_template("marketplace/search.html",
        results=paginated, q=q, category=category,
        sort=sort, min_price=min_price, max_price=max_price,
        total=total, page=page, pages=pages,
        categories=categories,
    )


@main_bp.route("/newsletter", methods=["POST"])
def newsletter():
    email = request.form.get("email", "").strip().lower()
    if not email or "@" not in email:
        flash("Please enter a valid email.", "danger")
    else:
        existing = db_select("newsletter_subscribers", filters={"email": email}, single=True)
        if not existing:
            db_insert("newsletter_subscribers", {"email": email})
            flash("You've subscribed to our newsletter! 🎉", "success")
        else:
            flash("You're already subscribed.", "info")
    return redirect(request.referrer or url_for("main.index"))


@main_bp.route("/about")
def about():
    return render_template("pages/about.html")


@main_bp.route("/terms")
def terms():
    return render_template("pages/terms.html")


@main_bp.route("/privacy")
def privacy():
    return render_template("pages/privacy.html")


@main_bp.route("/contact")
def contact():
    return render_template("pages/contact.html")
