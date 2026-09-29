"""Model-selection experiment: the same ~10 inputs against each model tier.

Runs in-process (no server needed) with the real agent loop and GEMINI_API_KEY
from .env. Writes evaluation/results/model_comparison.{json,md}.

    python evaluation/model_comparison.py --models gemini-3.1-flash-lite,gemini-3.6-flash
"""
import argparse, asyncio, json, statistics, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'evaluation'))
from app.arena import execute
from app.config import settings
from app.models import ArenaRequest
from checks import check_case, decision_stats, dumps, first_action

parser = argparse.ArgumentParser()
parser.add_argument('--models', default='gemini-3.1-flash-lite,gemini-3.6-flash')
parser.add_argument('--repeats', type=int, default=1)
args = parser.parse_args()
models = [m.strip() for m in args.models.split(',') if m.strip()]
settings.available_models = ','.join(models)  # allow every compared model for this process
cases = json.loads((ROOT / 'evaluation/model_comparison_cases.json').read_text(encoding='utf-8'))

def priority_ok(case, result):
    for ticket, want in case.get('required_priority', {}).items():
        if not any(e.get('event') == 'tool_result' and e.get('tool') == 'triage_ticket' and ticket in e.get('observation', '')
                   and f'"priority": "{want}"' in e.get('observation', '') for e in result['events']):
            return False
    return True

async def main():
    report = {'run_at': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'), 'models': {}}
    for model in models:
        rows = []
        for _ in range(args.repeats):
            for case in cases:
                req = ArenaRequest.model_validate({'arena_config': {'max_steps': 6, 'fault': 'none'}, **case['request']})
                result = (await execute(req, model=model)).model_dump()
                problems = check_case(case, result)
                if not priority_ok(case, result):
                    problems.append(f"priority not {case['required_priority']}")
                valid, total = decision_stats(result)
                first = first_action(result)
                rows.append({'case': case['name'], 'success': not problems, 'problems': problems,
                             'status': result['status'], 'first_action': first,
                             'action_ok': first in case['first_action'], 'valid_decisions': valid,
                             'total_decisions': total, 'latency_ms': result['metrics']['latency_ms'],
                             'model_calls': result['metrics']['model_calls'],
                             'input_tokens': result['metrics']['input_tokens'],
                             'output_tokens': result['metrics']['output_tokens'],
                             'cost_usd': result['metrics']['estimated_cost_usd']})
                print(f"{model:24} {'PASS' if not problems else 'FAIL'} {case['name']:28} {result['status']:20} "
                      f"first={first} {round(result['metrics']['latency_ms'])}ms {'; '.join(problems)}")
        n = len(rows)
        tot_dec = sum(r['total_decisions'] for r in rows)
        costs = [r['cost_usd'] for r in rows if r['cost_usd'] is not None]
        report['models'][model] = {
            'task_success': sum(r['success'] for r in rows) / n,
            'contract_validity': sum(r['valid_decisions'] for r in rows) / tot_dec if tot_dec else None,
            'action_selection': sum(r['action_ok'] for r in rows) / n,
            'avg_latency_ms': statistics.mean(r['latency_ms'] for r in rows),
            'avg_model_calls': statistics.mean(r['model_calls'] for r in rows),
            'avg_input_tokens': statistics.mean(r['input_tokens'] or 0 for r in rows),
            'avg_output_tokens': statistics.mean(r['output_tokens'] or 0 for r in rows),
            'avg_cost_usd_per_run': statistics.mean(costs) if costs else None,
            'runs': rows}
    out = ROOT / 'evaluation/results'
    out.mkdir(exist_ok=True)
    (out / 'model_comparison.json').write_text(dumps(report), encoding='utf-8')
    md = ['# Model comparison', '', f"Run at {report['run_at']} on {len(cases)} inputs x {args.repeats} repeat(s), max_steps=6, temperature 0.", '',
          '| Model | Task success | Contract validity | Correct first action | Avg latency / run | Avg model calls | Avg tokens in/out | Est. cost / run |',
          '|---|---|---|---|---|---|---|---|']
    for model, s in report['models'].items():
        cost = f"${s['avg_cost_usd_per_run']:.5f}" if s['avg_cost_usd_per_run'] is not None else 'n/a'
        validity = f"{s['contract_validity']:.0%}" if s['contract_validity'] is not None else 'n/a'
        md.append(f"| {model} | {s['task_success']:.0%} | {validity} | {s['action_selection']:.0%} | "
                  f"{s['avg_latency_ms']/1000:.2f}s | {s['avg_model_calls']:.1f} | {s['avg_input_tokens']:.0f}/{s['avg_output_tokens']:.0f} | {cost} |")
    md += ['', '## Per-case results', '', '| Model | Case | Pass | Status | First action | Valid/total decisions | Latency |', '|---|---|---|---|---|---|---|']
    for model, s in report['models'].items():
        for r in s['runs']:
            md.append(f"| {model} | {r['case']} | {'✅' if r['success'] else '❌'} | {r['status']} | {r['first_action']} | "
                      f"{r['valid_decisions']}/{r['total_decisions']} | {r['latency_ms']/1000:.2f}s |")
    (out / 'model_comparison.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    print('\n'.join(md[:4 + len(models) + 1]))

asyncio.run(main())
