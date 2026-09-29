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
parser.add_argument('--append', action='store_true', help='merge into the existing results file')
parser.add_argument('--pause', type=float, default=6, help='seconds between runs (free-tier rate limits)')
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
                await asyncio.sleep(args.pause)
                result = (await execute(req, model=model)).model_dump()
                problems = check_case(case, result)
                if not priority_ok(case, result):
                    problems.append(f"priority not {case['required_priority']}")
                valid, total = decision_stats(result)
                first = first_action(result)
                rows.append({'case': case['name'], 'success': not problems, 'problems': problems,
                             'status': result['status'], 'stop_reason': result['stop_reason'], 'first_action': first,
                             'action_ok': first in case['first_action'], 'valid_decisions': valid,
                             'total_decisions': total, 'latency_ms': result['metrics']['latency_ms'],
                             'model_calls': result['metrics']['model_calls'],
                             'input_tokens': result['metrics']['input_tokens'],
                             'output_tokens': result['metrics']['output_tokens'],
                             'cost_usd': result['metrics']['estimated_cost_usd']})
                print(f"{model:24} {'PASS' if not problems else 'FAIL'} {case['name']:28} {result['status']:20} "
                      f"first={first} {round(result['metrics']['latency_ms'])}ms {'; '.join(problems)}")
        report['models'][model] = {'runs': rows}
    out = ROOT / 'evaluation/results'
    out.mkdir(exist_ok=True)
    path = out / 'model_comparison.json'
    if args.append and path.exists():  # keep earlier models' runs, replace re-run ones
        previous = json.loads(path.read_text(encoding='utf-8'))
        report['models'] = {**previous['models'], **report['models']}
        report['run_at'] = previous['run_at'] + ' / ' + report['run_at']
    for model, s in report['models'].items():
        s.update(summarize(s['runs']))
    path.write_text(dumps(report), encoding='utf-8')
    md = ['# Model comparison', '', f"Runs at {report['run_at']}; {len(cases)} inputs per model, max_steps=6, temperature 0.",
          'Runs stopped by a provider quota/outage (`model_unavailable`, HTTP 429) are excluded from the quality metrics and counted separately.', '',
          '| Model | Scored runs | Task success | Contract validity | Correct first action | Avg latency / run | Avg model calls | Avg tokens in/out | Est. cost / run | Quota-blocked runs |',
          '|---|---|---|---|---|---|---|---|---|---|']
    for model, s in report['models'].items():
        if not s['scored']:
            md.append(f"| {model} | 0 | - | - | - | - | - | - | - | {s['blocked']} |"); continue
        cost = f"${s['avg_cost_usd_per_run']:.5f}" if s['avg_cost_usd_per_run'] is not None else 'n/a'
        validity = f"{s['contract_validity']:.0%}" if s['contract_validity'] is not None else 'n/a'
        md.append(f"| {model} | {s['scored']} | {s['task_success']:.0%} | {validity} | {s['action_selection']:.0%} | "
                  f"{s['avg_latency_ms']/1000:.2f}s | {s['avg_model_calls']:.1f} | {s['avg_input_tokens']:.0f}/{s['avg_output_tokens']:.0f} | {cost} | {s['blocked']} |")
    md += ['', '## Per-case results', '', '| Model | Case | Pass | Status | First action | Valid/total decisions | Latency |', '|---|---|---|---|---|---|---|']
    for model, s in report['models'].items():
        for r in s['runs']:
            mark = '⛔ quota' if blocked(r) else ('✅' if r['success'] else '❌')
            md.append(f"| {model} | {r['case']} | {mark} | {r['status']} | {r['first_action']} | "
                      f"{r['valid_decisions']}/{r['total_decisions']} | {r['latency_ms']/1000:.2f}s |")
    (out / 'model_comparison.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
    print('\n'.join(md[:6 + len(report['models'])]))

def blocked(r):
    # Older rows lack stop_reason; every 'failed' run in them was an HTTP 429 quota stop.
    return r.get('stop_reason', 'model_unavailable') == 'model_unavailable' and r['status'] == 'failed'

def summarize(rows):
    scored = [r for r in rows if not blocked(r)]
    n, tot_dec = len(scored), sum(r['total_decisions'] for r in scored)
    costs = [r['cost_usd'] for r in scored if r['cost_usd'] is not None]
    summary = {'scored': n, 'blocked': len(rows) - n}
    if n:
        summary.update({
            'task_success': sum(r['success'] for r in scored) / n,
            'contract_validity': sum(r['valid_decisions'] for r in scored) / tot_dec if tot_dec else None,
            'action_selection': sum(r['action_ok'] for r in scored) / n,
            'avg_latency_ms': statistics.mean(r['latency_ms'] for r in scored),
            'avg_model_calls': statistics.mean(r['model_calls'] for r in scored),
            'avg_input_tokens': statistics.mean(r['input_tokens'] or 0 for r in scored),
            'avg_output_tokens': statistics.mean(r['output_tokens'] or 0 for r in scored),
            'avg_cost_usd_per_run': statistics.mean(costs) if costs else None})
    return summary

asyncio.run(main())
