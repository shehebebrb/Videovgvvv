import os
import logging
from datetime import datetime, timedelta
from pymongo import MongoClient, ASCENDING, DESCENDING
from bson import ObjectId

logger = logging.getLogger(__name__)

MONGODB_URI = os.environ.get("MONGODB_URI")

client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
database = client["telegram_video_bot"]

users_col    = database["users"]
videos_col   = database["video_logs"]
payments_col = database["payments"]
vcache_col   = database["video_cache"]   # stores discovered video message IDs
config_col   = database["config"]        # stores bot config key-value pairs

try:
    users_col.create_index([("user_id", ASCENDING)], unique=True)
    videos_col.create_index([("user_id", ASCENDING), ("sent_at", DESCENDING)])
    payments_col.create_index([("user_id", ASCENDING)])
    payments_col.create_index([("status", ASCENDING)])
    vcache_col.create_index([("file_id", ASCENDING)], unique=True)
    logger.info("MongoDB indexes created.")
except Exception as e:
    logger.warning(f"Index creation warning: {e}")


# ─── CONFIG ────────────────────────────────────────────────────
def get_config(key: str):
    doc = config_col.find_one({"key": key})
    return doc["value"] if doc else None


def save_config(key: str, value: str):
    config_col.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value, "updated_at": datetime.utcnow()}},
        upsert=True
    )


# ─── VIDEO CACHE ───────────────────────────────────────────────
def add_video_to_cache(file_id: str, file_type: str = "video"):
    """Store a video by its Telegram file_id and type."""
    try:
        vcache_col.update_one(
            {"file_id": file_id},
            {"$setOnInsert": {"file_id": file_id, "file_type": file_type, "added_at": datetime.utcnow()}},
            upsert=True
        )
    except Exception as e:
        logger.error(f"Error adding video to cache: {e}")


def get_all_cached_videos() -> list:
    """Return list of dicts with file_id and file_type."""
    return list(vcache_col.find({}, {"file_id": 1, "file_type": 1, "_id": 0}))


def get_cached_video_count() -> int:
    return vcache_col.count_documents({})


def clear_video_cache():
    result = vcache_col.delete_many({})
    return result.deleted_count


def get_scan_state() -> dict:
    return database["scan_state"].find_one({"_id": "scan"}) or {}


def save_scan_state(last_scanned_id: int):
    database["scan_state"].update_one(
        {"_id": "scan"},
        {"$set": {"last_scanned_id": last_scanned_id, "updated_at": datetime.utcnow()}},
        upsert=True
    )


# ─── USERS ─────────────────────────────────────────────────────
def get_user(user_id: int):
    return users_col.find_one({"user_id": user_id})


def create_user(user_id: int, username: str, referrer_id: int = None):
    now = datetime.utcnow()
    doc = {
        "user_id": user_id,
        "username": username,
        "joined_at": now,
        "referral_count": 0,
        "referral_credits": 0,
        "referrer_id": referrer_id,
        "subscription_expiry": None,
        "expiry_warned": False,
        "subscription_status": "free",
    }
    try:
        users_col.insert_one(doc)
        if referrer_id:
            users_col.update_one(
                {"user_id": referrer_id},
                {"$inc": {"referral_count": 1}}
            )
    except Exception as e:
        logger.error(f"Error creating user {user_id}: {e}")


def get_all_user_ids() -> list:
    docs = users_col.find({}, {"user_id": 1})
    return [d["user_id"] for d in docs if d.get("user_id")]


def add_referral_credits(user_id: int, credits: int):
    users_col.update_one({"user_id": user_id}, {"$inc": {"referral_credits": credits}})


def use_referral_credit(user_id: int):
    users_col.update_one({"user_id": user_id}, {"$inc": {"referral_credits": -1}})


# ─── VIDEO LOGS ────────────────────────────────────────────────
def get_sent_video_file_ids(user_id: int) -> set:
    docs = videos_col.find({"user_id": user_id}, {"file_id": 1})
    return set(d["file_id"] for d in docs if d.get("file_id"))


def record_video_sent(user_id: int, file_id: str):
    videos_col.insert_one({
        "user_id": user_id,
        "file_id": file_id,
        "sent_at": datetime.utcnow()
    })


def count_videos_today(user_id: int, today_start: datetime) -> int:
    return videos_col.count_documents({
        "user_id": user_id,
        "sent_at": {"$gte": today_start}
    })


# ─── PAYMENTS ──────────────────────────────────────────────────
def create_payment_request(user_id: int, username: str, amount: str) -> str:
    doc = {
        "user_id": user_id,
        "username": username,
        "amount": amount,
        "status": "pending",
        "created_at": datetime.utcnow()
    }
    result = payments_col.insert_one(doc)
    return str(result.inserted_id)


def get_payment(payment_id: str):
    try:
        return payments_col.find_one({"_id": ObjectId(payment_id)})
    except Exception:
        return None


def get_pending_payments():
    return list(payments_col.find({"status": "pending"}).sort("created_at", ASCENDING))


def approve_payment(payment_id: str, user_id: int, sub_expiry: datetime):
    payments_col.update_one(
        {"_id": ObjectId(payment_id)},
        {"$set": {"status": "approved", "approved_at": datetime.utcnow()}}
    )
    users_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "subscription_expiry": sub_expiry,
            "expiry_warned": False,
            "subscription_status": "active"
        }}
    )


def reject_payment(payment_id: str):
    payments_col.update_one(
        {"_id": ObjectId(payment_id)},
        {"$set": {"status": "rejected", "rejected_at": datetime.utcnow()}}
    )


# ─── SUBSCRIPTIONS ─────────────────────────────────────────────
def get_expiring_subscriptions(before_time: datetime):
    now = datetime.utcnow()
    return list(users_col.find({
        "subscription_expiry": {"$gt": now, "$lte": before_time},
        "expiry_warned": {"$ne": True}
    }))


def mark_expiry_warned(user_id: int):
    users_col.update_one({"user_id": user_id}, {"$set": {"expiry_warned": True}})


def get_expired_subscriptions(now: datetime):
    two_hours_ago = now - timedelta(hours=2)
    return list(users_col.find({
        "subscription_expiry": {"$gte": two_hours_ago, "$lte": now},
        "subscription_status": "active"
    }))


def mark_subscription_expired(user_id: int):
    users_col.update_one({"user_id": user_id}, {"$set": {"subscription_status": "expired"}})


# ─── STATS ─────────────────────────────────────────────────────
def get_stats() -> dict:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "total_users":         users_col.count_documents({}),
        "active_subscribers":  users_col.count_documents({"subscription_expiry": {"$gt": now}}),
        "total_videos_sent":   videos_col.count_documents({}),
        "pending_payments":    payments_col.count_documents({"status": "pending"}),
        "today_new_users":     users_col.count_documents({"joined_at": {"$gte": today_start}}),
        "today_videos":        videos_col.count_documents({"sent_at": {"$gte": today_start}}),
        "cached_videos":       vcache_col.count_documents({})
    }


def get_recent_users(limit: int = 10):
    return list(users_col.find({}).sort("joined_at", DESCENDING).limit(limit))
