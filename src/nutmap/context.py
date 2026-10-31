import logging
import os
import time
import tomllib
from pathlib import Path

from filelock import FileLock, Timeout
from rich.console import Console
from rich.logging import RichHandler

from . import durable

log = logging.getLogger("nutmap")


class PipelineError(Exception):
    pass


class WaitingForApproval(Exception):
    pass


class Ctx:
    def __init__(self, config_path=None, data_dir=None):
        self.config_path = Path(config_path or os.environ.get("NUTMAP_CONFIG") or "config.toml").resolve()
        self.root = self.config_path.parent
        self.cfg = tomllib.loads(self.config_path.read_text("utf-8"))
        data = data_dir or os.environ.get("NUTMAP_DATA") or self.cfg["data_dir"]
        self.data = (self.root / data).resolve()
        self.console = Console(stderr=True)
        self.fake_llm = os.environ.get("NUTMAP_FAKE_LLM") == "1"
        self.fake_embed = os.environ.get("NUTMAP_FAKE_EMBED") == "1"
        self._lock = None
        self._angles = None
        self._client = None

    def path(self, *parts):
        return self.data.joinpath(*parts)

    def src(self, *parts):
        return self.root.joinpath(*parts)

    def angles(self):
        if self._angles is None:
            raw = tomllib.loads(self.src(self.cfg["angles_file"]).read_text("utf-8"))["angle"]
            wanted = self.cfg["subset"]["angles"]
            self._angles = [a for a in raw if not wanted or a["id"] in wanted]
        return self._angles

    def setup_logging(self, command):
        self.path("logs").mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        logfile = self.path("logs", f"{stamp}-{command}-{os.getpid()}.log")
        root = logging.getLogger("nutmap")
        root.setLevel(logging.INFO)
        root.handlers.clear()
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root.addHandler(fh)
        rh = RichHandler(console=self.console, show_path=False, show_time=False, markup=False)
        rh.setLevel(logging.INFO)
        root.addHandler(rh)
        self.logfile = logfile

    def acquire_lock(self):
        self.data.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.path(".lock")))
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            raise PipelineError(f"another nutmap run holds {self.path('.lock')}")
        durable.sweep_tmp(self.data)

    def client(self):
        if self._client is None:
            if self.fake_llm:
                from .fake_llm import FakeAnthropic

                self._client = FakeAnthropic(self.path("fake_server"))
            else:
                import anthropic

                self._client = anthropic.Anthropic(max_retries=4)
        return self._client

    def stamp_path(self, stage):
        return self.path(stage.dir, "_stamp.json")

    def read_stamp(self, stage):
        return durable.read_json(self.stamp_path(stage))

    def inputs_digest(self, stage):
        ups = {}
        for name in stage.needs:
            s = self.read_stamp(STAGES[name])
            if s is None:
                return None
            ups[name] = s["digest"]
        return durable.digest({"needs": ups, "config": stage.config_slice(self), "extra": stage.extra_inputs(self)})

    def stage_state(self, stage):
        d = self.inputs_digest(stage)
        s = self.read_stamp(stage)
        if s is not None and d is not None and s["digest"] == d and all(durable.exists(self.path(o)) for o in s["outputs"]):
            return "done"
        if s is not None:
            return "stale"
        if stage.started(self):
            return "partial"
        return "pending"

    def run_stage(self, stage, force=False):
        d = self.inputs_digest(stage)
        if d is None:
            missing = [n for n in stage.needs if self.read_stamp(STAGES[n]) is None]
            raise PipelineError(f"{stage.name}: upstream stages not done: {', '.join(missing)}")
        if not force and self.stage_state(stage) == "done":
            log.info("%s: up to date", stage.name)
            return
        log.info("%s: running", stage.name)
        t0 = time.time()
        stage.run(self)
        outputs = [str(Path(o)) for o in stage.outputs(self)]
        for o in outputs:
            if not durable.exists(self.path(o)):
                raise PipelineError(f"{stage.name}: missing output {o}")
        durable.atomic_write_json(self.stamp_path(stage), {"digest": d, "outputs": outputs, "seconds": round(time.time() - t0, 1)})
        log.info("%s: done in %.1fs", stage.name, time.time() - t0)


class Stage:
    name = ""
    dir = ""
    needs = ()

    def config_slice(self, ctx):
        return {}

    def extra_inputs(self, ctx):
        return {}

    def outputs(self, ctx):
        return []

    def started(self, ctx):
        return ctx.path(self.dir).exists() and any(ctx.path(self.dir).iterdir())

    def describe(self, ctx):
        return ""

    def run(self, ctx):
        raise NotImplementedError


STAGES = {}


def register(stage_cls):
    stage = stage_cls()
    STAGES[stage.name] = stage
    return stage_cls
