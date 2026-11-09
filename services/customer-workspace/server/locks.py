"""Cross-process disk-budget lock shared by uploads and same-machine backups."""
import contextlib
import os
import time
from pathlib import Path


class LockBusy(Exception): pass


@contextlib.contextmanager
def disk_budget(root,blocking=False,timeout=0):
    path=Path(root)/'.disk-budget.lock';path.parent.mkdir(parents=True,exist_ok=True)
    handle=path.open('a+b')
    try:
        deadline=time.monotonic()+timeout
        if os.name=='nt':
            import msvcrt
            handle.seek(0,os.SEEK_END)
            if handle.tell()==0:handle.write(b'0');handle.flush()
        else: import fcntl
        while True:
            try:
                if os.name=='nt':
                    handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,1)
                else:fcntl.flock(handle.fileno(),fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                break
            except OSError:
                if time.monotonic()>=deadline:raise LockBusy('disk budget is busy')
                time.sleep(.05)
        yield
    finally:
        handle.close()
