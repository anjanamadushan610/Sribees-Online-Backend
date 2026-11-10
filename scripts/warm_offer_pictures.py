"""
Draw every product's festival-offer picture ahead of shoppers.

A picture is otherwise drawn the first time a shopper's app asks for it (about
two seconds each, two at a time), so the first browse after the offer goes live
would be slow. Run once in the app container right after the deploy:

    docker exec <app container> python scripts/warm_offer_pictures.py [--workers 3]

Already-drawn pictures are skipped, so it is safe to run again. It reads the
database and the media store; it writes only the offer-cards cache folder.
"""
import argparse
import asyncio
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _draw(job):
    """In a worker process: draw one picture into the cache. Returns a status."""
    path, variant, target = job
    from app.api.offer_images import _design
    from app.services import offer_imagery
    from app.services.storage import get_storage

    target = Path(target)
    if target.is_file():
        return "cached"
    source = get_storage().read(path)
    if source is None:
        return "missing"
    try:
        picture = offer_imagery.render_offer_picture(source, _design(variant))
    except offer_imagery.ImageryError:
        return "unreadable"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(f".{os.getpid()}.part")
    partial.write_bytes(picture)
    partial.replace(target)
    return "drawn"


async def _jobs():
    from sqlalchemy import select

    from app.api.offer_images import _cache_dir, _key, _valid_path
    from app.config.database import async_session_maker
    from app.config.settings import settings
    from app.models.product import Product, ProductImage
    from app.services import festival_offer

    offer = festival_offer.active_offer()
    if offer is None:
        print("No festival offer is running; nothing to draw.")
        return []
    platform = float(settings.cashback_rate) * 100
    async with async_session_maker() as db:
        rows = (
            await db.execute(
                select(ProductImage.image_url, Product.cashback_percentage)
                .join(Product, Product.product_id == ProductImage.product_id)
                .where(Product.is_active.is_(True))
            )
        ).all()
    jobs, seen = [], set()
    for path, rate in rows:
        if not path or not _valid_path(path.strip()):
            continue
        path = path.strip()
        rate = platform if rate is None else float(rate)
        variant = "c" if float(festival_offer.apply_cashback(rate)) > 0 else "n"
        if (path, variant) in seen:
            continue
        seen.add((path, variant))
        jobs.append((path, variant, str(_cache_dir() / f"{_key(offer.code, variant, path)}.webp")))
    return jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    jobs = asyncio.run(_jobs())
    print(f"{len(jobs)} pictures to check")
    counts = {}
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_draw, job) for job in jobs]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                status = fut.result()
            except Exception as exc:  # one bad picture must not stop the rest
                status = f"error: {type(exc).__name__}"
            counts[status] = counts.get(status, 0) + 1
            if i % 50 == 0:
                print(f"  {i}/{len(jobs)} in {time.time() - started:.0f}s {counts}", flush=True)
    print(f"done in {time.time() - started:.0f}s: {counts}")


if __name__ == "__main__":
    main()
