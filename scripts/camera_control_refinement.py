#!/usr/bin/env python3
"""Check or refine one camera from fixed static-architecture source controls."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scene-core'))
from camera_control_fit import (load_camera_case,check_or_fit_camera,verify_camera_review,
                                atomic_json,sha256,_read_json,MultiviewError)
from multiview_fit import protect_output


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);commands=parser.add_subparsers(dest='command',required=True)
    for name in ('check','fit'):
        p=commands.add_parser(name);p.add_argument('--input',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p=commands.add_parser('review');p.add_argument('--report',required=True,type=Path);p.add_argument('--review',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        if args.command in ('check','fit'):
            case=load_camera_case(args.input)
            protect_output(args.output,[args.input]+[b['path'] for b in case['bindings']])
            report=check_or_fit_camera(case,optimize=args.command=='fit');atomic_json(args.output,report)
            print(json.dumps({'output':str(args.output),'status':report['status'],'frameId':report['frameId'],'reasons':report['reasons']},ensure_ascii=True))
            return 1 if report['status']=='REJECTED' else 0
        report=_read_json(args.report);review=_read_json(args.review)
        protect_output(args.output,[args.report,args.review]+[b['path'] for b in report.get('bindings',[])+review.get('bindings',[])])
        receipt=verify_camera_review(report,sha256(args.report),review);atomic_json(args.output,receipt)
        print(json.dumps(receipt,ensure_ascii=True));return 0 if receipt['acceptedWithinStatedControlUncertainty'] else 1
    except (MultiviewError,OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'error':getattr(exc,'code','INVALID_INPUT_OR_IO'),'message':str(exc),'details':getattr(exc,'details',{})},ensure_ascii=True),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
