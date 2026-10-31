import os

os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import argparse
import logging
import sys

from . import durable, embed, explain, export, flowmap, graph, inventory, layout, pack, panel, race, render, status, viewpoints, web
from .context import STAGES, Ctx, PipelineError, WaitingForApproval

log = logging.getLogger("nutmap")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m nutmap")
    ap.add_argument("command", choices=status.ORDER + ["run", "status", "approve", "pack"])
    ap.add_argument("--force", action="store_true", help="re-run the stage even if it is up to date")
    ap.add_argument("--remote", action="store_true", help="status: refresh batch counts from the API")
    ap.add_argument("--until", choices=status.ORDER, help="run: stop after this stage")
    args = ap.parse_args(argv)
    ctx = Ctx()
    if args.command == "status":
        status.show(ctx, remote=args.remote)
        return 0
    ctx.setup_logging(args.command)
    durable.install_signal_handlers()
    try:
        ctx.acquire_lock()
        if args.command == "approve":
            panel.approve(ctx)
        elif args.command == "pack":
            pack.pack(ctx)
        elif args.command == "run":
            for name in status.ORDER:
                ctx.run_stage(STAGES[name])
                if name == args.until:
                    break
            pack.pack(ctx)
            log.info("run: complete")
        else:
            ctx.run_stage(STAGES[args.command], force=args.force)
            pack.pack(ctx)
    except durable.Interrupted:
        log.warning("interrupted; re-run the same command to resume")
        return 130
    except WaitingForApproval as e:
        log.warning("%s", e)
        return 3
    except PipelineError as e:
        log.error("%s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
