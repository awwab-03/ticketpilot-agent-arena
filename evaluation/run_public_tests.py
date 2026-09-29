"""Runs evaluation/public_cases.json against a running server over HTTP.

Each call uses the configured model and may cost money. Results are written to
evaluation/results/public_results_<label>.json and .md.

    python evaluation/run_public_tests.py --url http://127.0.0.1:8000 --label local
    python evaluation/run_public_tests.py --url https://YOUR-APP.onrender.com --label deployed
"""
import argparse, json, sys, uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parent))
from checks import check_case, dumps

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8000')
parser.add_argument('--label', default='local')
parser.add_argument('--only', default='', help='comma-separated case-name prefixes, e.g. A1,B2')
args = parser.parse_args()
base = args.url.rstrip('/')
here = Path(__file__).resolve().parent
cases = json.loads((here / 'public_cases.json').read_text(encoding='utf-8'))
if args.only:
    cases = [c for c in cases if any(c['name'].startswith(p) for p in args.only.split(','))]

http = httpx.Client(timeout=90)
health = http.get(base + '/health').json()
model = http.get(base + '/models').json().get('default')
rows, passed = [], 0
for case in cases:
    try:
        if 'turns' in case:  # multi-turn: same session through the chat endpoint
            session, results, problems = 'eval-' + uuid.uuid4().hex, [], []
            for turn, expected in zip(case['turns'], case['expected_statuses']):
                r = http.post(base + '/chat', json={'session_id': session, 'task': turn, 'model': model})
                r.raise_for_status(); results.append(r.json())
                if results[-1]['status'] not in expected:
                    problems.append(f"turn {len(results)} status {results[-1]['status']} not in {expected}")
            used = {t['tool'] for t in results[-1]['tool_calls'] if t['outcome'] == 'success'}
            problems += [f'last turn did not use {t}' for t in case.get('expected_tools_last', []) if t not in used]
            http.delete(base + '/chat/' + session)
            result = results[-1]
            result['turn_statuses'] = [x['status'] for x in results]
            result['turn_responses'] = [x['final_response'] for x in results]
        else:
            r = http.post(base + '/arena/run', json=case['request'])
            r.raise_for_status(); result = r.json()
            problems = check_case(case, result)
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        result, problems = {}, [f'{type(exc).__name__}: {exc}']
    ok = not problems
    passed += ok
    print(('PASS ' if ok else 'FAIL ') + case['name'] + ('' if ok else '  <- ' + '; '.join(problems)))
    rows.append({'name': case['name'], 'category': case['category'], 'pass': ok, 'problems': problems,
                 'status': result.get('status'), 'stop_reason': result.get('stop_reason'),
                 'steps': result.get('steps'), 'tools': [f"{t['tool']}:{t['outcome']}" for t in result.get('tool_calls', [])],
                 'final_response': result.get('final_response'), 'metrics': result.get('metrics'),
                 'turn_statuses': result.get('turn_statuses'), 'turn_responses': result.get('turn_responses'),
                 'events': result.get('events')})

stamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
summary = {'url': base, 'label': args.label, 'run_at': stamp, 'model': model, 'health': health,
           'passed': passed, 'total': len(cases), 'cases': rows}
out = here / 'results'
out.mkdir(exist_ok=True)
(out / f'public_results_{args.label}.json').write_text(dumps(summary), encoding='utf-8')
md = [f'# Public test results ({args.label})', '', f'- URL: {base}', f'- Run at: {stamp}', f'- Model: {model}',
      f'- Passed: **{passed}/{len(cases)}**', '',
      '| Case | Cat | Pass | Status | Stop reason | Steps | Tools | Latency ms | Tokens in/out |', '|---|---|---|---|---|---|---|---|---|']
for r in rows:
    m = r['metrics'] or {}
    status = ' → '.join(r['turn_statuses']) if r['turn_statuses'] else r['status']
    md.append(f"| {r['name']} | {r['category']} | {'✅' if r['pass'] else '❌'} | {status} | {r['stop_reason']} | {r['steps']} | "
              f"{', '.join(r['tools']) or '-'} | {round(m.get('latency_ms') or 0)} | {m.get('input_tokens')}/{m.get('output_tokens')} |")
fails = [r for r in rows if not r['pass']]
if fails:
    md += ['', '## Failures', ''] + [f"- **{r['name']}**: {'; '.join(r['problems'])}" for r in fails]
(out / f'public_results_{args.label}.md').write_text('\n'.join(md) + '\n', encoding='utf-8')
print(f'{passed}/{len(cases)} public checks passed. Results: evaluation/results/public_results_{args.label}.md')
raise SystemExit(0 if passed == len(cases) else 1)
