import logging
import random
import db

logger = logging.getLogger(__name__)


def get_unseen_video(sent_file_ids: set) -> dict | None:
    """
    Pick a random video from the cache that the user hasn't seen yet.
    Returns a dict {file_id, file_type} or None.
    """
    all_cached = db.get_all_cached_videos()

    if not all_cached:
        logger.info("Cache is empty. No videos available.")
        return None

    unseen = [v for v in all_cached if v["file_id"] not in sent_file_ids]

    if not unseen:
        logger.info("User has seen all cached videos.")
        return None

    return random.choice(unseen)


async def send_video_to_user(bot, user_id: int, video_info: dict):
    """
    Send a video to a user using its file_id.
    Returns the sent Message object on success, or None on failure.
    """
    file_id   = video_info["file_id"]
    file_type = video_info.get("file_type", "video")

    try:
        if file_type == "animation":
            msg = await bot.send_animation(chat_id=user_id, animation=file_id)
        elif file_type == "video_note":
            msg = await bot.send_video_note(chat_id=user_id, video_note=file_id)
        elif file_type == "document":
            msg = await bot.send_document(chat_id=user_id, document=file_id)
        else:
            msg = await bot.send_video(chat_id=user_id, video=file_id)
        return msg
    except Exception as e:
        logger.error(f"Error sending video (file_id={file_id[:20]}...) to user {user_id}: {e}")
        return None
