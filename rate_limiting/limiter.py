from datetime import datetime, timezone, timedelta
import pytz
import logging

logger = logging.getLogger("Limiter-V4")
logger.propagate = False
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setLevel(logging.INFO)
handler.setFormatter(
    logging.Formatter('%(asctime)s [Limiter-V4] %(message)s'))
logger.addHandler(handler)

class LimitBlocked(Exception):

    def __init__(self, retry_after):
        self.retry_after = retry_after


class LimitHandler:

    def __init__(self, limits=None, span=None, max_=None):
        if limits:
            max_, span = [int(i) for i in limits]
        self.span = int(span)  # Duration of the bucket
        self.max = max(5, max_ - 5)  # Max Calls per bucket (Reduced by 1 for safety measures)
        self.count = 0  # Current Calls in this bucket
        self.bucket_start = datetime.now(timezone.utc)  # Cutoff after which no new requests are accepted
        self.bucket_end = self.bucket_start + timedelta(seconds=self.span + 1.5)  # EXTRA time when initiated
        self.bucket_reset_ready = self.bucket_start + timedelta(seconds=(self.span + 1.5) * 0.8)
        self.bucket_verifier = None

        logging.info(f"Initiated {self.max}:{self.span}.")

    def __repr__(self):

        return str(self.max + 5) + ":" + str(self.span)

    def __str__(self):

        return self.__repr__()

    @property
    def add(self):
        """Called before the request is made. Throws error if Limit is reached."""
        # (Re)set bucket if applicable
        if not self.bucket_start or self.bucket_end < datetime.now(timezone.utc):
            self.bucket_start = datetime.now(timezone.utc)
            self.bucket_end = self.bucket_start + timedelta(seconds=self.span + 1.5)  # EXTRA time when initiated
            self.bucket_reset_ready = self.bucket_start + timedelta(seconds=(self.span + 1.5) * 0.8)

            self.count = 0

        if self.count < self.max:
            self.count += 1
            return
        raise LimitBlocked(self.when_reset())

    def when_reset(self):
        """Return seconds until reset."""
        return int((self.bucket_end - datetime.now(timezone.utc)).total_seconds())

    async def update(self, date, limits):
        """Called with headers after the request."""
        for limit in limits.split(","):
            if int(limit.split(":")[1]) == self.span:
                count = int(limit.split(":")[0])
        naive = datetime.strptime(
            date,
            '%a, %d %b %Y %H:%M:%S GMT')
        local = pytz.timezone('GMT')
        local_dt = local.localize(naive, is_dst=None)
        date = local_dt.astimezone(pytz.utc)
        if count <= 5 and date > self.bucket_start:
            if date < self.bucket_reset_ready:
                if not self.bucket_verifier or self.bucket_verifier < count:
                    logger.info("Corrected bucket by %s.", (date - self.bucket_start).total_seconds())
                    self.bucket_start = date
                    self.bucket_end = self.bucket_start + timedelta(seconds=self.span)  # No extra time cause verified
                    self.bucket_reset_ready = self.bucket_start + timedelta(seconds=self.span * 0.8)
                    self.bucket_verifier = count
            else:
                logger.info("Initiated new bucket at %s.", date)
                self.bucket_start = date
                self.bucket_end = self.bucket_start + timedelta(
                    seconds=self.span)  # No extra time cause verified
                self.bucket_reset_ready = self.bucket_start + timedelta(seconds=self.span * 0.8)
                self.bucket_verifier = count
                self.count = count
        elif count > 5 and date > self.bucket_start:
            if count > self.count:
                self.count = count
