#!/usr/bin/env python3
"""Inspect, fit and independently review source-bound multi-view cuboids."""
from pathlib import Path
import argparse,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scene-core'))
from multiview_fit import (MultiviewError,load_bundle,fit_bundle,inspect_bundle,
                          atomic_json,verify_review,sha256,_read_json,render_report,protect_output)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('inspect','fit'):
        p=commands.add_parser(name);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=commands.add_parser('report');p.add_argument('--fit-report',type=Path,required=True);p.add_argument('--review',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p=commands.add_parser('render');p.add_argument('--input',type=Path,required=True);p.add_argument('--fit-report',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--panel-width',type=int,default=800)
    args=parser.parse_args(argv)
    try:
        if args.command in ('inspect','fit'):
            bundle=load_bundle(args.input)
            protect_output(args.output,[args.input]+[r['path'] for r in bundle['bindings']])
            result=inspect_bundle(bundle) if args.command=='inspect' else fit_bundle(bundle)
            atomic_json(args.output,result)
            print(json.dumps({'output':str(args.output),'status':result['status'],'objects':len(result['objects']),
                              'allObjectsProducedCandidates':result.get('allObjectsProducedCandidates')},ensure_ascii=True))
            return 0 if args.command=='inspect' or result['allObjectsProducedCandidates'] else 1
        if args.command=='render':
            bundle=load_bundle(args.input);report=_read_json(args.fit_report)
            protect_output(args.output_dir,[args.input,args.fit_report]+[b['path'] for b in bundle['bindings']+report.get('bindings',[])],directory=True)
            result=render_report(bundle,report,args.output_dir,args.panel_width)
            print(json.dumps(result,ensure_ascii=True));return 0
        report=_read_json(args.fit_report);review=_read_json(args.review)
        protect_output(args.output,[args.fit_report,args.review]+[b['path'] for b in report.get('bindings',[])+review.get('bindings',[])])
        result=verify_review(report,sha256(args.fit_report),review);atomic_json(args.output,result)
        print(json.dumps(result,ensure_ascii=True));return 0 if result['allObjectsReviewedAndAccepted'] else 1
    except (MultiviewError,OSError,KeyError,TypeError,ValueError) as exc:
        print(json.dumps({'error':getattr(exc,'code','INVALID_INPUT_OR_IO'),'message':str(exc),
                          'details':getattr(exc,'details',{})},ensure_ascii=True),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
