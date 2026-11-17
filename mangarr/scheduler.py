"""Planificateur intégré (APScheduler) : vérification périodique, intervalle réglable."""
from apscheduler.schedulers.background import BackgroundScheduler

from mangarr import catalog
from mangarr.core.engine import RunOptions, check_all
from mangarr.tomes import covers

JOB_ID = "check_all"


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.sched = BackgroundScheduler(timezone=ctx.settings.timezone, job_defaults={
            "max_instances": 1, "coalesce": True, "misfire_grace_time": 300})

    def interval(self):
        """Minutes : réglage en base, sinon variable d'environnement. 0 = désactivé."""
        return int(self.ctx.repo.get_setting("check_interval_min", self.ctx.settings.check_interval_min))

    def _job(self):
        check_all(self.ctx, opts=RunOptions())

    def start(self):
        self.sched.start()
        self.reschedule()
        # catalogues des sources (24 h) et jaquettes manquantes : indépendants de l'intervalle de vérification
        self.sched.add_job(lambda: catalog.refresh(self.ctx, stale_only=True), "interval", hours=12, id="catalog")
        self.sched.add_job(lambda: covers.fill_missing(self.ctx), "interval", hours=6, id="covers")

    def reschedule(self):
        minutes = self.interval()
        if self.sched.get_job(JOB_ID):
            self.sched.remove_job(JOB_ID)
        if minutes > 0:
            self.sched.add_job(self._job, "interval", minutes=minutes, id=JOB_ID)

    def set_interval(self, minutes):
        self.ctx.repo.set_setting("check_interval_min", int(minutes))
        self.reschedule()

    def run_now_async(self, only=None):
        self.sched.add_job(lambda: check_all(self.ctx, only=only, opts=RunOptions()))

    def stop(self):
        self.sched.shutdown(wait=False)
