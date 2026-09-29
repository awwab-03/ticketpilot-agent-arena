const $ = id => document.getElementById(id);
let session = crypto.randomUUID(), count = 0;

function message(role, text, status) {
  const p = document.createElement('article');
  p.className = role;
  p.textContent = (role === 'user' ? 'You' : 'Agent' + (status ? ' [' + status + ']' : '')) + ': ' + text;
  $('messages').append(p);
  p.scrollIntoView({block: 'nearest'});
}

function describe(e) {
  switch (e.event) {
    case 'agent_start': return `start · max_steps=${e.max_steps} · fault=${e.fault} · model=${e.model}`;
    case 'model_call': return `model call · ${e.latency_ms} ms · tokens in/out ${e.input_tokens ?? '?'}/${e.output_tokens ?? '?'}`;
    case 'fault_injected': return `FAULT injected: ${e.fault}`;
    case 'decision': return `decision ✓ ${e.status}${e.action ? ' → ' + e.action + ' ' + e.arguments : ''}`;
    case 'decision_rejected': return `decision ✗ rejected: ${e.violations.join(' | ')}`;
    case 'repair_requested': return `repair requested (${e.attempt}/${e.limit})`;
    case 'tool_result': return `${e.tool} → ${e.ok ? 'ok' : 'failed'} [${e.outcomes.join(', ')}] · ${e.observation}`;
    case 'autonomy_gate': return `autonomy gate: ${e.action} → ${e.decision}`;
    case 'policy_override': return `policy override: ${e.reason}`;
    case 'agent_stop': return `stop · ${e.status} · ${e.reason}`;
    default: return JSON.stringify(e);
  }
}

function render(data) {
  $('status').textContent = `${data.status} · ${data.stop_reason} · ${data.steps} step(s)`;
  $('status').className = 'status-' + data.status;
  const m = data.metrics;
  $('metrics').textContent = `latency ${Math.round(m.latency_ms)} ms · model calls ${m.model_calls} · tokens ${m.input_tokens ?? 'n/a'}/${m.output_tokens ?? 'n/a'} · est. cost ${m.estimated_cost_usd == null ? 'n/a' : '$' + m.estimated_cost_usd.toFixed(6)}`;
  $('timeline').replaceChildren(...data.events.map(e => {
    const li = document.createElement('li');
    li.textContent = `[step ${e.step}] ` + describe(e);
    li.className = 'ev-' + e.event;
    return li;
  }));
  $('trace').textContent = data.tool_calls.length
    ? data.tool_calls.map(t => `step ${t.step} · ${t.tool} · attempt ${t.attempt} · ${t.outcome} · ${Math.round(t.latency_ms)} ms`).join('\n')
    : 'No tool calls.';
  $('errors').textContent = data.errors.length ? JSON.stringify(data.errors, null, 2) : 'None.';
}

async function init() {
  try {
    const r = await fetch('/models');
    if (!r.ok) throw Error('Models unavailable');
    const data = await r.json();
    for (const m of data.models) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m + (m === data.default ? ' (default)' : '');
      $('model').append(o);
    }
    if (data.models[0] === 'unconfigured') $('status').textContent = 'No model configured on the server.';
  } catch (e) { $('status').textContent = e.message; $('send').disabled = true; }
}

$('chat').onsubmit = async e => {
  e.preventDefault();
  const task = $('task').value.trim();
  if (!task) return;
  const note = $('external').value.trim();
  message('user', task);
  $('send').disabled = $('reset').disabled = true;
  $('status').textContent = 'Running…'; $('status').className = '';
  $('timeline').replaceChildren();
  try {
    const r = await fetch('/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      session_id: session, task, model: $('model').value,
      external_context: note ? [{source: 'browser-note', content: note, trust: 'untrusted'}] : [],
      arena_config: {max_steps: Number($('steps').value), fault: $('fault').value}})});
    const data = await r.json();
    if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
    message('agent', data.final_response, data.status);
    count += 2;
    $('memory').textContent = `${count} messages exchanged; the server keeps the last 12`;
    render(data);
    $('task').value = '';
  } catch (err) {
    $('status').textContent = 'Error: ' + err.message;
  } finally {
    $('send').disabled = $('reset').disabled = false;
  }
};

$('reset').onclick = async () => {
  try {
    const r = await fetch('/chat/' + session, {method: 'DELETE'});
    if (!r.ok) throw Error('Reset failed');
    session = crypto.randomUUID(); count = 0;
    $('messages').replaceChildren();
    $('memory').textContent = '0 messages in this chat';
    $('external').value = $('task').value = '';
    $('status').textContent = 'Idle'; $('status').className = ''; $('metrics').textContent = '';
    $('timeline').innerHTML = '<li>No run yet.</li>';
    $('trace').textContent = 'No tool calls yet.'; $('errors').textContent = 'None.';
  } catch (err) { $('status').textContent = err.message; }
};
init();
