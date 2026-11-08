"""Offline parsed-plan routing benchmark. Never reads real accounts or uses upstream.

python scripts/benchmark_routing.py --output logs/tests/routing-performance.json
"""
import argparse
import json
import platform
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import qoder_routing as routing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', default='b16b5c8')
    parser.add_argument('--output')
    args = parser.parse_args()
    before = types.ModuleType('routing_reference')
    source = subprocess.check_output(['git', 'show', args.reference+':qoder_routing.py'], cwd=ROOT).decode('utf-8')
    exec(compile(source, '<routing reference>', 'exec'), before.__dict__)
    accounts = [types.SimpleNamespace(uid='synthetic-'+str(i), group='work', realm='cn') for i in range(1000)]
    with tempfile.TemporaryDirectory(prefix='qd-routing-bench-') as directory:
        routing.save_config(directory, tiers=[{'id':str(i), 'accounts':['synthetic-'+str(j) for j in range(i*16,(i+1)*16)]} for i in range(60)], policy=routing.DEFAULT_POLICY)
        def measure(module):
            samples=[]
            for _ in range(7):
                start=time.perf_counter_ns()
                for n in range(500):
                    decision=module.resolve(directory,accounts,'Qwen',key={'id':'synthetic-key'})
                samples.append((time.perf_counter_ns()-start)/1e6/500)
            assert decision['allowed_uids']=={'synthetic-'+str(i) for i in range(960)}
            return round(statistics.median(samples),4)
        old=measure(before);new=measure(routing)
    result={'reference':args.reference,'python':platform.python_version(),'platform':platform.platform(),
            'workload':'1000 synthetic accounts, 60 tiers, 500 calls x 7 rounds; median per call; no upstream',
            'before_ms':old,'after_ms':new,'speedup':round(old/new,2)}
    text=json.dumps(result,ensure_ascii=False,indent=2)
    print(text)
    if args.output:
        Path(args.output).write_text(text+'\n',encoding='utf-8')


if __name__=='__main__':
    main()
