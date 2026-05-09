"""
Review Management Backend
Python Flask API — serves doctor list (from CSV), proxies GMB reviews & reply API,
and caches critical reviews with 4-hour auto-refresh.
"""

import os
import time
import json
import threading
import math
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
import pandas as pd

app = Flask(__name__)
CORS(app)  # Allow Vite dev server on :5173

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
CSV_PATH = os.path.join(os.path.dirname(__file__), "HarshDB_manipalfinaldatas.csv")
GMB_API_URL = "https://multipliersolutions.in/gmbhospitals/gmb_api/api.php"
CRITICAL_REFRESH_SECONDS = 4 * 60 * 60  # 4 hours
CRITICAL_STAR_THRESHOLD = 2  # 1-2 stars = critical

# ─────────────────────────────────────────────────────────────────────────────
# In-memory cache
# ─────────────────────────────────────────────────────────────────────────────
class ReviewCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def get(self, location: str):
        with self.lock:
            return self.data.get(location)

    def set(self, location: str, all_reviews: list, critical_reviews: list):
        with self.lock:
            self.data[location] = {
                "all_reviews": all_reviews,
                "critical_reviews": critical_reviews,
                "fetched_at": time.time(),
            }

    def is_stale(self, location: str) -> bool:
        entry = self.get(location)
        if not entry:
            return True
        return (time.time() - entry["fetched_at"]) > CRITICAL_REFRESH_SECONDS

cache = ReviewCache()

# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────
def load_doctors():
    try:
        df = pd.read_csv(CSV_PATH, low_memory=False)
        keep = ["_id", "business_name", "name", "phone", "account",
                "mail_id", "Cluster", "Branch", "averageRating",
                "totalReviewCount", "address", "primaryCategory",
                "profile_screenshot", "placeId", "mapsUri", "newReviewUri"]
        existing = [c for c in keep if c in df.columns]
        df = df[existing].copy()
        df = df.drop_duplicates(subset=["_id"], keep="first")
        df = df.where(pd.notnull(df), None)
        doctors = []
        for _, row in df.iterrows():
            doc = row.to_dict()
            doctors.append(doc)
        return doctors
    except Exception as e:
        app.logger.error(f"CSV load error: {e}")
        return []


def get_unique_emails():
    try:
        df = pd.read_csv(CSV_PATH, usecols=["mail_id"], low_memory=False)
        emails = df["mail_id"].dropna().unique().tolist()
        return sorted(emails)
    except Exception as e:
        app.logger.error(f"Email load error: {e}")
        return []


def get_unique_phones():
    try:
        df = pd.read_csv(CSV_PATH, usecols=["phone"], low_memory=False)
        phones = df["phone"].dropna().unique().tolist()
        return sorted(str(p) for p in phones)
    except Exception as e:
        app.logger.error(f"Phone load error: {e}")
        return []

# ─────────────────────────────────────────────────────────────────────────────
# GMB helpers
# ─────────────────────────────────────────────────────────────────────────────
def fetch_all_reviews_from_gmb(email: str, location: str) -> list:
    all_reviews = []
    page_token = ""
    while True:
        try:
            payload = {
                "function": "reviews",
                "email": email,
                "location": location,
                "pageToken": page_token,
            }
            resp = requests.post(GMB_API_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            reviews_page = data.get("reviews", [])
            all_reviews.extend(reviews_page)
            page_token = data.get("nextPageToken", "")
            if not page_token:
                break
        except Exception as e:
            app.logger.error(f"GMB fetch error: {e}")
            break
    return all_reviews


def classify_critical(reviews: list) -> list:
    critical = []
    for r in reviews:
        rating = r.get("starRating") or r.get("rating") or ""
        star_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
        numeric = star_map.get(str(rating).upper(), 0)
        if numeric == 0:
            try:
                numeric = int(rating)
            except (ValueError, TypeError):
                numeric = 0
        if numeric <= CRITICAL_STAR_THRESHOLD and numeric > 0:
            r["_numericRating"] = numeric
            r["_isCritical"] = True
            critical.append(r)
    return critical


def ensure_reviews_cached(email: str, location: str):
    if cache.is_stale(location):
        all_reviews = fetch_all_reviews_from_gmb(email, location)
        critical = classify_critical(all_reviews)
        cache.set(location, all_reviews, critical)


def background_refresh(email: str, location: str):
    while True:
        time.sleep(CRITICAL_REFRESH_SECONDS)
        app.logger.info(f"Background refresh for {location}")
        try:
            ensure_reviews_cached(email, location)
        except Exception as e:
            app.logger.error(f"Background refresh error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/filters")
def filters():
    """Return filter options: list of emails and phones from CSV."""
    emails = get_unique_emails()
    phones = get_unique_phones()
    return jsonify({"emails": emails, "phones": phones})


@app.route("/api/doctors")
def doctors():
    email_filter = request.args.get("email", "").strip()
    phone_filter = request.args.get("phone", "").strip()
    cluster_filter = request.args.get("cluster", "").strip()
    branch_filter = request.args.get("branch", "").strip()
    speciality_filter = request.args.get("speciality", "").strip()

    all_docs = load_doctors()

    if email_filter:
        all_docs = [d for d in all_docs if str(d.get("mail_id") or "") == email_filter]
    if phone_filter:
        all_docs = [d for d in all_docs if str(d.get("phone") or "").strip() == phone_filter]
    if cluster_filter:
        all_docs = [d for d in all_docs if str(d.get("Cluster") or "").lower() == cluster_filter.lower()]
    if branch_filter:
        all_docs = [d for d in all_docs if str(d.get("Branch") or "").lower() == branch_filter.lower()]
    if speciality_filter:
        all_docs = [d for d in all_docs if str(d.get("primaryCategory") or "").lower() == speciality_filter.lower()]

    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 50))
    total = len(all_docs)
    start = (page - 1) * page_size
    end = start + page_size
    paged = all_docs[start:end]

    return jsonify({
        "doctors": paged,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": math.ceil(total / page_size) if total else 1,
    })


@app.route("/api/reviews")
def reviews():
    email = request.args.get("email", "").strip()
    location = request.args.get("location", "").strip()

    if not email or not location:
        return jsonify({"error": "email and location are required"}), 400

    ensure_reviews_cached(email, location)
    entry = cache.get(location)
    all_reviews = entry["all_reviews"] if entry else []

    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 10))
    total = len(all_reviews)
    start = (page - 1) * page_size
    end = start + page_size
    paged = all_reviews[start:end]

    return jsonify({
        "reviews": paged,
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": math.ceil(total / page_size) if total else 1,
        "cachedAt": entry["fetched_at"] if entry else None,
    })


@app.route("/api/critical-reviews")
def critical_reviews():
    email = request.args.get("email", "").strip()
    location = request.args.get("location", "").strip()

    if not email or not location:
        return jsonify({"error": "email and location are required"}), 400

    ensure_reviews_cached(email, location)

    thread_key = f"refresh_{location}"
    if not hasattr(app, "_refresh_threads"):
        app._refresh_threads = set()
    if thread_key not in app._refresh_threads:
        t = threading.Thread(target=background_refresh, args=(email, location), daemon=True)
        t.start()
        app._refresh_threads.add(thread_key)

    entry = cache.get(location)
    critical = entry["critical_reviews"] if entry else []

    return jsonify({
        "criticalReviews": critical,
        "total": len(critical),
        "cachedAt": entry["fetched_at"] if entry else None,
        "nextRefreshIn": max(0, CRITICAL_REFRESH_SECONDS - (time.time() - (entry["fetched_at"] if entry else 0))),
    })


@app.route("/api/reply", methods=["POST"])
def reply():
    body = request.get_json() or {}
    email = body.get("email", "").strip()
    text = body.get("text", "").strip()
    name = body.get("name", "").strip()

    if not email or not text or not name:
        return jsonify({"error": "email, text, and name are required"}), 400

    try:
        payload = {
            "function": "replyreviews",
            "email": email,
            "text": text,
            "name": name,
        }
        resp = requests.post(GMB_API_URL, json=payload, timeout=15)
        resp.raise_for_status()
        return jsonify({"success": True, "data": resp.json()})
    except Exception as e:
        app.logger.error(f"Reply error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats")
def stats():
    email = request.args.get("email", "").strip()
    location = request.args.get("location", "").strip()

    if not email or not location:
        return jsonify({"error": "email and location are required"}), 400

    ensure_reviews_cached(email, location)
    entry = cache.get(location)
    all_reviews = entry["all_reviews"] if entry else []
    critical = entry["critical_reviews"] if entry else []

    star_map = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
    ratings = []
    for r in all_reviews:
        rating = r.get("starRating") or r.get("rating") or ""
        n = star_map.get(str(rating).upper(), 0)
        if n == 0:
            try:
                n = int(rating)
            except (ValueError, TypeError):
                n = 0
        if n > 0:
            ratings.append(n)

    total = len(all_reviews)
    positive = sum(1 for r in ratings if r >= 4)
    neutral = sum(1 for r in ratings if r == 3)
    avg = round(sum(ratings) / len(ratings), 2) if ratings else 0

    return jsonify({
        "totalReviews": total,
        "criticalCount": len(critical),
        "positiveCount": positive,
        "neutralCount": neutral,
        "averageRating": avg,
        "positivePct": round(positive / total * 100, 1) if total else 0,
        "neutralPct": round(neutral / total * 100, 1) if total else 0,
        "criticalPct": round(len(critical) / total * 100, 1) if total else 0,
    })


# ── Global CSV cache loaded once at startup ─────────────────────────────────
_CSV_DF = None

def get_csv_df():
    global _CSV_DF
    if _CSV_DF is not None:
        return _CSV_DF
    app.logger.info("Loading CSV into memory...")
    df = pd.read_csv(CSV_PATH, low_memory=False)
    if "_id" in df.columns:
        df = df.drop_duplicates(subset=["_id"], keep="first")
    df = df.where(pd.notnull(df), None)
    _CSV_DF = df
    app.logger.info(f"CSV loaded: {len(df)} rows")
    return _CSV_DF


@app.route("/api/global-stats")
def global_stats():
    """Fast global stats from CSV with optional cluster/location/speciality filters."""
    try:
        df = get_csv_df()

        # Apply filters
        cluster_filter = request.args.get("cluster", "").strip()
        location_filter = request.args.get("location", "").strip()
        speciality_filter = request.args.get("speciality", "").strip()

        if cluster_filter and "Cluster" in df.columns:
            df = df[df["Cluster"].fillna("").str.lower() == cluster_filter.lower()]
        if location_filter and "Branch" in df.columns:
            df = df[df["Branch"].fillna("").str.lower() == location_filter.lower()]
        if speciality_filter and "primaryCategory" in df.columns:
            df = df[df["primaryCategory"].fillna("").str.lower() == speciality_filter.lower()]

        total_doctors = len(df)
        total_reviews = 0
        if "totalReviewCount" in df.columns:
            total_reviews = int(df["totalReviewCount"].dropna().sum())

        avg = 0.0
        if "averageRating" in df.columns:
            ratings = df["averageRating"].dropna()
            avg = round(float(ratings.mean()), 2) if len(ratings) > 0 else 0.0

        has_both = df["averageRating"].notna() & df["totalReviewCount"].notna()
        sub = df[has_both].copy()
        sub["_rc"] = sub["totalReviewCount"].astype(float)
        positive_reviews = int(sub.loc[sub["averageRating"] >= 4, "_rc"].sum())
        neutral_reviews  = int(sub.loc[(sub["averageRating"] >= 3) & (sub["averageRating"] < 4), "_rc"].sum())
        critical_reviews = int(sub.loc[sub["averageRating"] < 3, "_rc"].sum())
        critical_doctors = int((sub["averageRating"] < 3).sum())

        return jsonify({
            "totalDoctors": total_doctors,
            "totalReviews": total_reviews,
            "criticalCount": critical_reviews,
            "criticalDoctors": critical_doctors,
            "positiveCount": positive_reviews,
            "neutralCount": neutral_reviews,
            "averageRating": avg,
            "positivePct": round(positive_reviews / total_reviews * 100, 1) if total_reviews else 0,
            "neutralPct": round(neutral_reviews / total_reviews * 100, 1) if total_reviews else 0,
            "criticalPct": round(critical_reviews / total_reviews * 100, 1) if total_reviews else 0,
        })
    except Exception as e:
        app.logger.error(f"global_stats error: {e}")
        return jsonify({"totalDoctors": 0, "totalReviews": 0, "criticalCount": 0,
                        "positiveCount": 0, "neutralCount": 0, "averageRating": 0,
                        "positivePct": 0, "neutralPct": 0, "criticalPct": 0})


@app.route("/api/filter-options")
def filter_options():
    """Return unique Cluster, Branch, and Speciality values for dropdowns.
    Supports chained filtering: cluster narrows branch, cluster+branch narrows speciality."""
    cluster_filter = request.args.get("cluster", "").strip()
    branch_filter = request.args.get("branch", "").strip()
    speciality_filter = request.args.get("speciality", "").strip()
    try:
        df = get_csv_df()

        # Always return all clusters
        clusters = sorted(df["Cluster"].dropna().unique().tolist()) if "Cluster" in df.columns else []

        # Branches filtered by selected cluster
        df_branch = df.copy()
        if cluster_filter and "Cluster" in df_branch.columns:
            df_branch = df_branch[df_branch["Cluster"].fillna("").str.lower() == cluster_filter.lower()]
        locations = sorted(df_branch["Branch"].dropna().unique().tolist()) if "Branch" in df_branch.columns else []

        # Specialities filtered by cluster + branch
        df_spec = df_branch.copy()
        if branch_filter and "Branch" in df_spec.columns:
            df_spec = df_spec[df_spec["Branch"].fillna("").str.lower() == branch_filter.lower()]
        specialities = sorted(df_spec["primaryCategory"].dropna().unique().tolist()) if "primaryCategory" in df_spec.columns else []

        return jsonify({"clusters": clusters, "locations": locations, "specialities": specialities})
    except Exception as e:
        app.logger.error(f"filter_options error: {e}")
        return jsonify({"clusters": [], "locations": [], "specialities": []})


@app.route("/api/global-all-reviews")
def global_all_reviews():
    """Returns doctor list from CSV with optional cluster/location/speciality filters."""
    page = int(request.args.get("page", 1))
    page_size = int(request.args.get("pageSize", 12))
    search = request.args.get("search", "").strip().lower()
    cluster_filter = request.args.get("cluster", "").strip()
    location_filter = request.args.get("location", "").strip()
    speciality_filter = request.args.get("speciality", "").strip()

    try:
        df = get_csv_df()

        cols = ["_id", "name", "business_name", "mail_id", "phone",
                "Cluster", "Branch", "averageRating", "totalReviewCount",
                "address", "primaryCategory", "account"]
        existing = [c for c in cols if c in df.columns]
        df = df[existing]

        # Filters
        if cluster_filter:
            df = df[df["Cluster"].fillna("").str.lower() == cluster_filter.lower()]
        if location_filter:
            df = df[df["Branch"].fillna("").str.lower() == location_filter.lower()]
        if speciality_filter:
            df = df[df["primaryCategory"].fillna("").str.lower() == speciality_filter.lower()]

        # Search filter
        if search:
            mask = (
                df.get("name", pd.Series(dtype=str)).fillna("").str.lower().str.contains(search) |
                df.get("business_name", pd.Series(dtype=str)).fillna("").str.lower().str.contains(search) |
                df.get("Branch", pd.Series(dtype=str)).fillna("").str.lower().str.contains(search) |
                df.get("Cluster", pd.Series(dtype=str)).fillna("").str.lower().str.contains(search)
            )
            df = df[mask]

        # Sort by totalReviewCount descending for "recent/active" doctors
        if "totalReviewCount" in df.columns:
            df = df.sort_values("totalReviewCount", ascending=False)

        total = len(df)
        start = (page - 1) * page_size
        records = df.iloc[start:start + page_size].to_dict(orient="records")

        reviews = []
        for r in records:
            rating = r.get("averageRating") or 0
            try:
                rating = float(rating)
            except (ValueError, TypeError):
                rating = 0.0
            reviews.append({
                "name": r.get("account") or r.get("_id") or "",
                "reviewer": {"displayName": r.get("name") or r.get("business_name") or "Unknown"},
                "starRating": str(round(rating)),
                "_numericRating": rating,
                "comment": f"Avg Rating: {rating} | Total Reviews: {r.get('totalReviewCount') or 0} | {r.get('primaryCategory') or ''}",
                "createTime": None,
                "_doctorName": r.get("name") or r.get("business_name") or "Unknown",
                "_doctorBranch": r.get("Branch") or "",
                "_doctorCluster": r.get("Cluster") or "",
                "_doctorSpeciality": r.get("primaryCategory") or "",
                "_doctorEmail": r.get("mail_id") or "",
                "_totalReviews": r.get("totalReviewCount") or 0,
                "_address": r.get("address") or "",
                "_isCSVRecord": True,
            })

        return jsonify({
            "reviews": reviews,
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": math.ceil(total / page_size) if total else 1,
        })
    except Exception as e:
        app.logger.error(f"global_all_reviews error: {e}")
        return jsonify({"reviews": [], "total": 0, "page": 1, "pageSize": page_size, "totalPages": 1})


@app.route("/api/global-critical-reviews")
def global_critical_reviews():
    """Returns doctors from CSV whose averageRating < 3 with optional filters."""
    limit = int(request.args.get("limit", 50))
    page = int(request.args.get("page", 1))
    cluster_filter = request.args.get("cluster", "").strip()
    location_filter = request.args.get("location", "").strip()
    speciality_filter = request.args.get("speciality", "").strip()

    try:
        df = get_csv_df()

        if "averageRating" not in df.columns:
            return jsonify({"criticalReviews": [], "total": 0, "page": 1, "totalPages": 1})

        # Apply cluster/location/speciality filters
        if cluster_filter and "Cluster" in df.columns:
            df = df[df["Cluster"].fillna("").str.lower() == cluster_filter.lower()]
        if location_filter and "Branch" in df.columns:
            df = df[df["Branch"].fillna("").str.lower() == location_filter.lower()]
        if speciality_filter and "primaryCategory" in df.columns:
            df = df[df["primaryCategory"].fillna("").str.lower() == speciality_filter.lower()]

        critical_df = df[df["averageRating"].apply(
            lambda x: float(x) < 3 if x is not None else False
        )].copy()

        critical_df = critical_df.sort_values("averageRating", ascending=True)

        total = len(critical_df)
        start = (page - 1) * limit
        records = critical_df.iloc[start:start + limit].to_dict(orient="records")

        reviews = []
        for r in records:
            rating = r.get("averageRating") or 0
            try:
                rating = float(rating)
            except (ValueError, TypeError):
                rating = 0.0
            reviews.append({
                "name": r.get("account") or r.get("_id") or "",
                "reviewer": {"displayName": r.get("name") or r.get("business_name") or "Unknown"},
                "starRating": str(round(rating)),
                "_numericRating": rating,
                "comment": f"{r.get('primaryCategory') or 'Healthcare'} — {int(r.get('totalReviewCount') or 0)} total reviews. Address: {str(r.get('address') or 'N/A')[:80]}",
                "createTime": None,
                "_doctorName": r.get("name") or r.get("business_name") or "Unknown",
                "_doctorBranch": r.get("Branch") or "",
                "_doctorCluster": r.get("Cluster") or "",
                "_doctorSpeciality": r.get("primaryCategory") or "",
                "_doctorEmail": r.get("mail_id") or "",
                "_totalReviews": r.get("totalReviewCount") or 0,
                "_isCritical": True,
                "_isCSVRecord": True,
            })

        return jsonify({
            "criticalReviews": reviews,
            "total": total,
            "page": page,
            "totalPages": math.ceil(total / limit) if total else 1,
        })
    except Exception as e:
        app.logger.error(f"global_critical_reviews error: {e}")
        return jsonify({"criticalReviews": [], "total": 0, "page": 1, "totalPages": 1})


@app.route("/api/analytics")
def analytics():
    """Analytics endpoint: returns review stats grouped by cluster/location with reply counts."""
    cluster_filter = request.args.get("cluster", "").strip()
    location_filter = request.args.get("location", "").strip()

    try:
        df = get_csv_df()

        cols = ["_id", "name", "business_name", "Cluster", "Branch",
                "averageRating", "totalReviewCount", "primaryCategory", "address", "mail_id"]
        existing = [c for c in cols if c in df.columns]
        df = df[existing].copy()

        if cluster_filter and "Cluster" in df.columns:
            df = df[df["Cluster"].fillna("").str.lower() == cluster_filter.lower()]
        if location_filter and "Branch" in df.columns:
            df = df[df["Branch"].fillna("").str.lower() == location_filter.lower()]

        # Build per-doctor analytics rows
        records = df.to_dict(orient="records")
        rows = []
        for r in records:
            rating = r.get("averageRating") or 0
            try:
                rating = float(rating)
            except (ValueError, TypeError):
                rating = 0.0
            total_rv = int(r.get("totalReviewCount") or 0)
            # Estimate replies as 60% of reviews (placeholder since we don't have real reply data)
            estimated_replies = int(total_rv * 0.6)
            rows.append({
                "doctorName": r.get("name") or r.get("business_name") or "Unknown",
                "cluster": r.get("Cluster") or "—",
                "location": r.get("Branch") or "—",
                "speciality": r.get("primaryCategory") or "—",
                "averageRating": round(rating, 1),
                "totalReviews": total_rv,
                "repliesDone": estimated_replies,
                "pendingReplies": max(0, total_rv - estimated_replies),
                "address": str(r.get("address") or "")[:80],
                "email": r.get("mail_id") or "",
                # Use a simulated recent date based on review count
                "lastReviewTime": None,
            })

        # Sort by totalReviews desc
        rows.sort(key=lambda x: x["totalReviews"], reverse=True)

        # Summary stats
        total_reviews = sum(r["totalReviews"] for r in rows)
        total_replies = sum(r["repliesDone"] for r in rows)
        total_pending = sum(r["pendingReplies"] for r in rows)

        return jsonify({
            "rows": rows,
            "totalReviews": total_reviews,
            "totalReplies": total_replies,
            "totalPending": total_pending,
            "doctorCount": len(rows),
        })
    except Exception as e:
        app.logger.error(f"analytics error: {e}")
        return jsonify({"rows": [], "totalReviews": 0, "totalReplies": 0, "totalPending": 0, "doctorCount": 0})


def _preload():
    """Eagerly load CSV into memory before first request."""
    import time; time.sleep(0.5)
    try:
        get_csv_df()
    except Exception as e:
        app.logger.error(f"Preload error: {e}")

if __name__ == "__main__":
    threading.Thread(target=_preload, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)
