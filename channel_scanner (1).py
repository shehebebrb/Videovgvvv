import asyncio
import logging
import db

logger = logging.getLogger(__name__)

CONSECUTIVE_FAIL_LIMIT = 100   # stop after this many consecutive missing IDs
SCAN_DELAY = 0.15              # seconds between each probe (rate limiting)


async def probe_scan(bot, source_channel_id: int, admin_id: int) -> int:
    """
    Scan the source channel by forwarding each message to admin DM,
    checking if it's a video, caching the ID, then deleting the copy.
    Returns the number of new videos found.
    """
    if not source_channel_id:
        logger.warning("probe_scan: source_channel_id is 0, skipping.")
        return 0

    scan_state  = db.get_scan_state()
    start_id    = scan_state.get("last_scanned_id", 0) + 1
    msg_id      = start_id
    consecutive_fails = 0
    new_videos  = 0

    logger.info(f"Probe scan starting from message_id={start_id} in channel {source_channel_id}")

    while consecutive_fails < CONSECUTIVE_FAIL_LIMIT:
        try:
            fwd = await bot.forward_message(
                chat_id=admin_id,
                from_chat_id=source_channel_id,
                message_id=msg_id
            )
            consecutive_fails = 0
            db.save_scan_state(msg_id)

            # Determine if the forwarded message is a video
            is_video = (
                fwd.video is not None
                or fwd.animation is not None
                or fwd.video_note is not None
                or (
                    fwd.document is not None
                    and fwd.document.mime_type is not None
                    and "video" in fwd.document.mime_type
                )
            )

            if is_video:
                db.add_video_to_cache(msg_id)
                new_videos += 1
                logger.info(f"Cached video msg_id={msg_id}  (total={db.get_cached_video_count()})")

            # Always delete the forwarded copy from admin DM
            try:
                await bot.delete_message(chat_id=admin_id, message_id=fwd.message_id)
            except Exception:
                pass

        except Exception as e:
            err = str(e).lower()
            if "message to forward not found" in err or "invalid" in err:
                consecutive_fails += 1
            else:
                # Unexpected error — pause a bit and retry same ID
                logger.warning(f"Scan error at msg_id={msg_id}: {e}")
                await asyncio.sleep(2)
                continue

        msg_id += 1
        await asyncio.sleep(SCAN_DELAY)

    logger.info(
        f"Probe scan finished. Scanned up to msg_id={msg_id}. "
        f"New videos cached: {new_videos}."
    )
    return new_videos
