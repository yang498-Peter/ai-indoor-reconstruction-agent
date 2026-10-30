#!/usr/bin/env python3
"""Fit a frozen object registry without dropping unobserved or rejected cases."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scene-core'))
from multiview_batch import load_job,write_batch


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job',required=True,type=Path);parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        report=write_batch(load_job(args.job),args.output_dir)
        print(json.dumps({k:report[k] for k in ('status','logicalObjectDenominator','requiredRoomDenominator','wholeObjectAcceptedCount','counts','modelApplied')}))
        return 0 if report['logicalObjectDenominator'] and report['wholeObjectAcceptedCount']==report['logicalObjectDenominator'] else 1
    except Exception as exc:
        print(json.dumps({'error':getattr(exc,'code','BATCH_INPUT_OR_IO_ERROR'),'exceptionType':type(exc).__name__}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
