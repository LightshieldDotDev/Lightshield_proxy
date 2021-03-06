import asyncio
import logging
from datetime import datetime, timezone, timedelta

import pytz

class LimitBlocked(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after


class LimitHandler:
    bucket = False
    blocked = False
    verified = 99
    reset_ready = False
    bucket_start = None
    bucket_end = None
    count = 0
    bucket_task_reset = None
    bucket_task_reset_verified = None

    def __init__(self, server, limits=None, span=None, max_=None, method="app"):

        self.type = method
        self.logging = logging
        if limits:
            max_, span = [int(i) for i in limits]
        self.span = int(span)  # Duration of the bucket
        self.max = max(
            5, max_ - 2
        )  # Max Calls per bucket (Reduced by some for safety measures)
        self.logging.info(f"Initiated %s with %s:%s.", self.type, self.max, self.span)
        self.init_lock = asyncio.Lock()
        self.verify_lock = asyncio.Lock()

        self.logging = logging.getLogger("limit_%s_%s_%s" % (server, method, span))
        self.logging.propagate = False
        self.logging.setLevel(logging.INFO)
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(f"%(asctime)s [{server.upper()}:{method}:{span}] %(message)s"))
        self.logging.addHandler(handler)


    def __repr__(self):

        return str(self.max + 2) + ":" + str(self.span)

    def __str__(self):

        return self.__repr__()

    async def init_bucket(self, pre_verified=None, verified_count=0):
        """Create a new bucket.

        The bucket is unverified by default but can be started verified if its initialized by a delayed request.
        """
        if self.bucket:
            return
        if self.bucket_task_reset:
            self.bucket_task_reset.cancel()
        if self.bucket_task_reset_verified:
            self.bucket_task_reset_verified.cancel()
        duration = self.span

        if not pre_verified:
            duration = self.span + max(0.5, self.span * 0.1)
            self.bucket_start = datetime.now(timezone.utc)
            self.verified = 99
            self.bucket_task_reset = asyncio.get_event_loop().call_later(
                duration, self.destroy_bucket
            )
        else:
            self.verified = verified_count
            self.bucket_start = pre_verified
        self.bucket_end = self.bucket_start + timedelta(seconds=duration)
        self.bucket_end = self.bucket_start + timedelta(seconds=duration)

        self.bucket = True
        self.blocked = False
        self.reset_ready = datetime.now(timezone.utc) + timedelta(
            seconds=duration * 0.8
        )
        self.bucket_task_reset_verified = asyncio.get_event_loop().call_later(
            self.span, self.destroy_bucket
        )
        self.logging.info(
            "Initiated new bucket at %s. [previous %s: %s/%s][%s]",
            self.bucket_start,
            self.type,
            self.count,
            self.max,
            pre_verified is None,
        )
        self.count = 0

    async def verify_bucket(self, verified_start, verified_count):
        """Verify an existing buckets starting point.

        Removes the extra 20% duration added as safety net.
        """
        if verified_count > self.verified:
            return
        self.logging.debug(
            "Verifying bucket [%s -> %s].",
            self.verified,
            verified_count,
        )

        self.verified = verified_count
        self.bucket_start = verified_start
        self.bucket_end = self.bucket_start + timedelta(seconds=self.span)
        if self.bucket_end <= (now := datetime.now(timezone.utc)):
            self.bucket = False
            self.logging.debug("Verified bucket. Was overdo.")
            return

    def destroy_bucket(self, verify=False):
        """Mark the bucket as destroyed."""
        if verify and not self.verified:
            pass
        if self.bucket_task_reset:
            self.bucket_task_reset.cancel()
        self.bucket = False
        self.blocked = False
        self.logging.info(
            "Destroyed bucket [Verified: %s].", self.verified
        )

    async def add(self):
        """Called before the request is made. Throws error if Limit is reached."""
        # If already blocked throw exception
        if self.blocked:
            raise LimitBlocked(
                int((self.bucket_end - datetime.now(timezone.utc)).total_seconds())
            )

        # If no active bucket create a new one
        if not self.bucket:
            async with self.init_lock:
                if not self.bucket:
                    await self.init_bucket()

        self.count += 1
        # If count reaches/breaches max, block.
        if self.count >= self.max:
            self.logging.info("Blocking bucket.")
            self.blocked = True

    async def update(self, date, limits):
        """Called with headers after the request."""
        count = None
        for limit in limits.split(","):
            if int(limit.split(":")[1]) == self.span:
                count = int(limit.split(":")[0])
        naive = datetime.strptime(date, "%a, %d %b %Y %H:%M:%S GMT")
        local = pytz.timezone("GMT")
        local_dt = local.localize(naive, is_dst=None)
        date = local_dt.astimezone(pytz.utc)

        if count <= 10:
            # If no bucket create one
            if not self.bucket:
                async with self.init_lock:
                    await self.init_bucket(pre_verified=date, verified_count=count)
            # If bucket is ready to be reset, reset.
            elif self.reset_ready < date:
                async with self.init_lock:
                    await self.init_bucket(pre_verified=date, verified_count=count)
            # If its a new request, update verification
            elif self.verified > count:
                async with self.verify_lock:
                    await self.verify_bucket(verified_start=date, verified_count=count)

        if count > self.count:
            self.count = count
            if self.count >= self.max:
                self.blocked = True
