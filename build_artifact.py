"""Compose the leadership brief as a single self-contained artifact page."""
import json, pathlib

D = json.load(open("exec_summary.json"))

HTML = """<title>Why our agents are slow</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Source+Serif+4:ital,opsz,wght@0,8..60,400;0,8..60,600;1,8..60,400&display=swap">
<style>
  :root{
    --bg:#f2f4f6; --panel:#fff; --panel-2:#e9edf1;
    --ink:#14181d; --mid:#4d5560; --muted:#7d8794;
    --line:#dde2e8; --line-soft:#eaeef2;
    --ok:#1e6b4c; --ok-soft:#dcece4;
    --stop:#c62f27; --stop-soft:#f7dedb;
    --warn:#9a6206;
    --display:"Barlow",system-ui,sans-serif;
    --body:"Source Serif 4",Georgia,serif;
    --data:"IBM Plex Mono",ui-monospace,monospace;
  }
  @media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
    --bg:#14171b; --panel:#1b1f25; --panel-2:#232830;
    --ink:#eef1f4; --mid:#b3bcc6; --muted:#828d9a;
    --line:#2e343d; --line-soft:#22272e;
    --ok:#4fb086; --ok-soft:#16302639;
    --stop:#f0665a; --stop-soft:#3a1c1a;
    --warn:#d9a13d;
  }}
  :root[data-theme="dark"]{
    --bg:#14171b; --panel:#1b1f25; --panel-2:#232830;
    --ink:#eef1f4; --mid:#b3bcc6; --muted:#828d9a;
    --line:#2e343d; --line-soft:#22272e;
    --ok:#4fb086; --ok-soft:#163026;
    --stop:#f0665a; --stop-soft:#3a1c1a;
    --warn:#d9a13d;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font-family:var(--body);
    font-size:17px;line-height:1.66;-webkit-font-smoothing:antialiased}
  .wrap{max-width:960px;margin:0 auto;padding:0 28px 88px}
  .col{max-width:660px}
  h1,h2,h3,.eyebrow,.stat b,.chip,.tag,th,.gauge-pct{font-family:var(--display)}
  .eyebrow,.tag,.cap,th,.mono{font-family:var(--data)}

  .eyebrow{font-size:11.5px;letter-spacing:.16em;text-transform:uppercase;
    color:var(--muted);font-weight:500}
  header{padding:72px 0 0}
  h1{font-size:clamp(34px,5.4vw,54px);line-height:1.04;font-weight:700;letter-spacing:-0.025em;
    margin:20px 0 22px;max-width:14ch;text-wrap:balance}
  .lede{font-size:20px;line-height:1.58;color:var(--mid);max-width:60ch;margin:0}
  .lede b{color:var(--ink);font-weight:600}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));
    gap:1px;background:var(--line);border:1px solid var(--line);margin:44px 0 0}
  .stat{background:var(--panel);padding:20px 22px 18px}
  .stat b{display:block;font-size:38px;font-weight:600;line-height:1;letter-spacing:-0.03em;
    font-variant-numeric:tabular-nums}
  .stat .cap{display:block;font-size:11.5px;line-height:1.45;color:var(--muted);margin-top:9px;
    letter-spacing:.02em}
  .stat.stop b{color:var(--stop)}
  .stat.ok b{color:var(--ok)}

  .rule{height:1px;background:var(--line);margin:56px 0}
  h2{font-size:27px;font-weight:600;letter-spacing:-0.02em;line-height:1.2;margin:0 0 12px;
    text-wrap:balance}
  h3{font-size:18px;font-weight:600;margin:0 0 6px;letter-spacing:-0.01em}
  p{margin:0 0 18px;color:var(--mid)}
  .col p{max-width:62ch}
  b,strong{color:var(--ink);font-weight:600}

  /* instrument panel */
  .panel{background:var(--panel);border:1px solid var(--line);padding:28px 30px;margin:30px 0}
  .gauge+.gauge{margin-top:26px}
  .gauge-head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;margin-bottom:10px}
  .gauge-name{font-family:var(--display);font-size:17px;font-weight:600}
  .gauge-name span{font-family:var(--body);font-weight:400;color:var(--muted);font-size:15px}
  .gauge-pct{font-size:21px;font-weight:600;font-variant-numeric:tabular-nums}
  .track{height:30px;background:var(--panel-2);position:relative;overflow:hidden}
  .fill{height:100%;transition:width .6s cubic-bezier(.4,0,.2,1)}
  .ticks{position:absolute;inset:0;pointer-events:none}
  .ticks i{position:absolute;top:0;bottom:0;width:1px;background:var(--line);opacity:.85}
  .gauge-note{font-size:14.5px;color:var(--muted);margin-top:9px;line-height:1.5}

  figure{margin:34px 0 0}
  figcaption{font-size:14.5px;color:var(--muted);margin-top:14px;line-height:1.55;max-width:64ch}
  svg{display:block;width:100%;height:auto}
  .key{display:flex;gap:22px;flex-wrap:wrap;font-size:13px;margin-bottom:14px;color:var(--mid);
    font-family:var(--data)}
  .key i{display:inline-block;width:16px;height:3px;vertical-align:middle;margin-right:8px}

  .note{border-left:3px solid var(--stop);background:var(--panel);padding:20px 24px;margin:30px 0}
  .note .eyebrow{color:var(--stop);margin-bottom:9px;display:block}
  .note p{margin:0;max-width:62ch}

  .band{font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);
    font-family:var(--data);display:flex;align-items:center;gap:13px;margin:34px 0 14px}
  .band::after{content:"";flex:1;height:1px;background:var(--line)}
  .cards{display:grid;gap:12px}
  .card{background:var(--panel);border:1px solid var(--line);padding:20px 23px;
    border-left:3px solid var(--stop)}
  .card.out{border-left-color:var(--muted)}
  .card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:8px}
  .tag{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;font-weight:600;
    padding:4px 10px;white-space:nowrap;flex-shrink:0}
  .tag.live{background:var(--stop-soft);color:var(--stop)}
  .tag.out{background:var(--panel-2);color:var(--muted)}
  .card p{margin:0;font-size:16px}

  .cmp{border-top:1px solid var(--line);margin:28px 0 0}
  .cmp-row{display:grid;grid-template-columns:1fr auto;gap:22px;align-items:center;
    padding:15px 0;border-bottom:1px solid var(--line-soft)}
  .cmp-name{font-size:16px;min-width:0}
  .cmp-name em{display:block;font-style:normal;font-size:13px;color:var(--muted);
    font-family:var(--data);margin-top:3px}
  .bars{display:flex;flex-direction:column;gap:7px}
  .bar-line{display:flex;align-items:center;gap:11px}
  .bar-track{flex:0 0 96px;height:15px;background:var(--panel-2)}
  .bar-fill{height:100%;min-width:2px}
  .bar-line span{font-family:var(--data);font-size:12px;color:var(--mid);white-space:nowrap;
    font-variant-numeric:tabular-nums}
  @media (max-width:620px){.cmp-row{grid-template-columns:1fr;gap:11px}.bar-track{flex:0 0 76px}}

  ol.asks{counter-reset:s;list-style:none;padding:0;margin:28px 0 0;max-width:64ch}
  ol.asks li{counter-increment:s;position:relative;padding:0 0 26px 56px}
  ol.asks li::before{content:counter(s);position:absolute;left:0;top:-2px;width:34px;height:34px;
    border:1px solid var(--ink);color:var(--ink);display:grid;place-items:center;
    font-family:var(--display);font-size:16px;font-weight:600}
  ol.asks li::after{content:"";position:absolute;left:17px;top:40px;bottom:2px;width:1px;
    background:var(--line)}
  ol.asks li:last-child{padding-bottom:0}
  ol.asks li:last-child::after{display:none}
  ol.asks p{margin:0;font-size:16px}
  .cost{display:block;font-family:var(--data);font-size:12px;color:var(--muted);margin-top:9px;
    letter-spacing:.02em}

  details{margin-top:26px;border-top:1px solid var(--line);padding-top:18px}
  summary{cursor:pointer;font-family:var(--display);font-size:16px;font-weight:600;
    color:var(--ink);list-style:none}
  summary::-webkit-details-marker{display:none}
  summary::before{content:"+";display:inline-block;margin-right:11px;color:var(--stop);font-weight:600}
  details[open] summary::before{content:"\\2212"}
  summary:focus-visible{outline:2px solid var(--stop);outline-offset:3px}
  .scroll{overflow-x:auto;margin-top:16px}
  table{border-collapse:collapse;width:100%;font-family:var(--data);font-size:13.5px;
    font-variant-numeric:tabular-nums}
  th,td{text-align:right;padding:9px 12px;border-bottom:1px solid var(--line-soft);white-space:nowrap}
  th:first-child,td:first-child,th:last-child,td:last-child{text-align:left}
  th{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--muted);font-weight:500}
  tr.hit td{color:var(--stop)}

  .limits{background:var(--panel);border:1px dashed var(--line);padding:26px 28px}
  .limits ul{margin:0;padding-left:20px;color:var(--mid);font-size:16px}
  .limits li{margin-bottom:10px}.limits li:last-child{margin-bottom:0}
  footer{margin-top:52px;padding-top:24px;border-top:1px solid var(--line);
    font-family:var(--data);font-size:12.5px;color:var(--muted);line-height:1.7}
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>

<div class="wrap">
<header>
  <div class="eyebrow">Agent platform &middot; latency investigation &middot; findings</div>
  <h1>Why our agents are slow</h1>
  <p class="lede">Our monitoring tracks <b>how many calls</b> we send to the AI provider, and that number looks healthy. But the provider also limits <b>how much text</b> we send, and that is the budget we believe we are exhausting. The dashboard shows nothing wrong because it is not watching the gauge that matters.</p>
  <div class="stats" id="stats"></div>
</header>

<div class="rule"></div>

<section>
  <div class="col">
    <h2>The same moment, two different budgets</h2>
    <p>Both readings below are taken at the busiest moment of a simulated run of our own traffic pattern. The provider enforces both limits independently. We only monitor the first.</p>
  </div>
  <div class="panel" id="gauges"></div>

  <div class="note">
    <span class="eyebrow">Why this hides so well</span>
    <p>Agents run about ten steps each, and every step re-sends the whole conversation so far. Our text volume therefore grows far faster than our call count. A single call at step ten can carry many times the text of the same call at step one &mdash; but a call counter records both as one.</p>
  </div>

  <figure>
    <div class="key">
      <span><i style="background:var(--muted)"></i>Call budget used</span>
      <span><i style="background:var(--stop)"></i>Text budget used</span>
      <span style="color:var(--muted)">&middot; &middot; &middot; the limit</span>
    </div>
    <div id="timeline"></div>
    <figcaption>Ten minutes of simulated traffic. The grey line is what we currently monitor. The red line is what we believe is actually stopping us: it reaches the ceiling in every busy period and stays there until the budget refills.</figcaption>
  </figure>
</section>

<div class="rule"></div>

<section>
  <div class="col">
    <h2>What could be causing it</h2>
    <p>We started with five candidate explanations and tested each against what we actually observe. Four remain possible; one is ruled out. We have deliberately not narrowed further &mdash; the remaining four need different fixes, and nothing we have yet can tell them apart.</p>
  </div>
  <div id="candidates"></div>
</section>

<div class="rule"></div>

<section>
  <div class="col">
    <h2>Our retries are helping, not hurting</h2>
    <p>We went in assuming our automatic retries were making things worse. The testing says the opposite. When the provider refuses a call, retrying is what eventually gets it through &mdash; <b>without retries, a large share of calls simply fail instead.</b></p>
    <p>But retrying is also where the long wait comes from. Each attempt waits longer than the last, so a call that succeeds on its seventh attempt spent most of that time waiting between tries. <b>The 40-second wait is not the provider being slow. It is us queuing politely.</b></p>
  </div>

  <div class="cmp" id="retries"></div>

  <div class="note">
    <span class="eyebrow">The trade we are actually making</span>
    <p>Turning retries down would shorten the worst waits and convert them into outright failures. That is a product decision rather than a technical one: <em>is a call that takes 40 seconds better or worse than a call that never happens?</em> For most of our agents we think slow beats failed &mdash; but it should be a choice we make on purpose.</p>
  </div>

  <details>
    <summary>How the worst-case wait grows with each extra retry</summary>
    <p style="margin-top:16px;max-width:62ch">Each row is the longest wait our worst-affected calls could reach at a given number of attempts, while typical calls still complete in about a second. This is how we estimate how many retries are really happening in production today.</p>
    <div class="scroll"><table id="waitTable"></table></div>
  </details>
</section>

<div class="rule"></div>

<section>
  <div class="col">
    <h2>What we are asking for</h2>
    <p>Three steps, in order. The first is small and unblocks the rest.</p>
  </div>
  <ol class="asks">
    <li>
      <h3>Add two numbers to our monitoring</h3>
      <p>We already collect most of what we need: the provider reports our remaining budget on every single response, and we are saving it. We need two more fields &mdash; how much of our conversation history the provider is reusing for free, and a clean count of refused calls. That is the difference between guessing and knowing.</p>
      <span class="cost">Small change to the shared client &middot; unblocks the decision below</span>
    </li>
    <li>
      <h3>Read the answer from real traffic</h3>
      <p>With those fields in place, the four remaining explanations become distinguishable within days of normal running. No experiment and no load test &mdash; we simply look.</p>
      <span class="cost">No engineering work &middot; one analysis pass</span>
    </li>
    <li>
      <h3>Then turn on traffic metering</h3>
      <p>A service every call passes through that paces our requests, the same idea as the traffic lights on a motorway on-ramp. It is built and tested, and currently runs in observe-only mode: it records what it <em>would</em> have done without changing anything.</p>
      <span class="cost">Built already &middot; stays observe-only until step 2 answers</span>
    </li>
  </ol>

  <div class="note">
    <span class="eyebrow">Why not just turn it on now</span>
    <p>One of the five explanations &mdash; a bottleneck inside our own service &mdash; would be made <em>worse</em> by pacing our traffic, not better. We ruled it out by reasoning rather than by measurement. Until step 2 confirms that, switching metering on blind carries a real risk of spending effort to make things slower.</p>
  </div>
</section>

<div class="rule"></div>

<section class="limits">
  <h3>What this does not tell us yet</h3>
  <ul>
    <li><b>These numbers come from a simulation, not from production.</b> We modelled the provider's published rules and our own traffic shape. It narrows five explanations to four and tells us what to measure. It does not tell us which one is true.</li>
    <li><b>Several inputs are estimates.</b> Where we did not know a number we tested a range of values rather than picking one. Findings that hold across the whole range are marked as such in the technical detail; those that do not are flagged provisional.</li>
    <li><b>More than one cause may be true at once.</b> We are not expecting a single culprit.</li>
    <li><b>We built this to be able to prove ourselves wrong.</b> The test harness includes checks confirming it can return the inconvenient answers &mdash; including "nothing is wrong at the provider, the problem is ours" and "the proposed fix makes things worse".</li>
  </ul>
</section>

<footer>
  Simulated against the provider's published rate-limit and caching rules.<br>
  Figures are modelled, not measured in production. Prepared by the agent platform team.
</footer>
</div>

<script>
const D = __DATA__;
const G = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const pc = v => Math.round(v * 100) + "%";

document.getElementById('stats').innerHTML = [
  [pc(D.paradox.peak_calls_used), 'of the call budget used at peak', 'ok'],
  [pc(D.paradox.peak_tokens_used), 'of the text budget used at peak', 'stop'],
  [D.paradox.typical_seconds + 's', 'a typical call', ''],
  [D.paradox.slowest_seconds + 's', 'the slowest 1 in 100 calls', 'stop'],
].map(([v, c, k]) => `<div class="stat ${k}"><b>${v}</b><span class="cap">${c}</span></div>`).join('');

document.getElementById('gauges').innerHTML = [
  { n:'Calls sent', s:'what our dashboard measures', v:D.paradox.peak_calls_used, c:'--ok',
    note:'Comfortably inside the limit. This is the number that has been reassuring us.' },
  { n:'Text sent', s:'measured in tokens, roughly words', v:D.paradox.peak_tokens_used, c:'--stop',
    note:'Fully consumed. Calls beyond this point are refused and have to be retried.' },
].map(g => `
  <div class="gauge">
    <div class="gauge-head">
      <span class="gauge-name">${g.n} <span>&mdash; ${g.s}</span></span>
      <span class="gauge-pct" style="color:var(${g.c})">${pc(g.v)}</span>
    </div>
    <div class="track">
      <div class="fill" style="width:${Math.min(100, g.v*100)}%;background:var(${g.c})"></div>
      <div class="ticks">${[25,50,75,100].map(t=>`<i style="left:${t}%"></i>`).join('')}</div>
    </div>
    <div class="gauge-note">${g.note}</div>
  </div>`).join('');

(function timeline(){
  const s = D.paradox.series, W=900, H=230, P={l:46,r:16,t:16,b:30};
  const xmax = Math.max(...s.map(p=>p.t),1);
  const sx = t => P.l + (t/xmax)*(W-P.l-P.r);
  const sy = v => P.t + (1-Math.min(v,1.06)/1.06)*(H-P.t-P.b);
  let g = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Call budget and text budget consumed over ten minutes of simulated traffic">`;
  [0,0.5,1].forEach(v=>{
    g += `<line x1="${P.l}" y1="${sy(v)}" x2="${W-P.r}" y2="${sy(v)}" stroke="${v===1?G('--stop'):G('--line')}"${v===1?' stroke-dasharray="3 5" opacity=".8"':''}/>`;
    g += `<text x="8" y="${sy(v)+4}" fill="${G('--muted')}" font-size="11" font-family="${G('--data')}">${v*100}%</text>`;
  });
  const line=(k,c,w)=>`<path d="${s.map((p,i)=>`${i?'L':'M'}${sx(p.t).toFixed(1)},${sy(p[k]).toFixed(1)}`).join('')}" fill="none" stroke="${c}" stroke-width="${w}" stroke-linejoin="round"/>`;
  g += line('calls_used', G('--muted'), 1.7) + line('tokens_used', G('--stop'), 2.4);
  g += `<text x="${P.l}" y="${H-8}" fill="${G('--muted')}" font-size="11" font-family="${G('--data')}">START</text>`;
  g += `<text x="${W-P.r}" y="${H-8}" text-anchor="end" fill="${G('--muted')}" font-size="11" font-family="${G('--data')}">10 MINUTES</text></svg>`;
  document.getElementById('timeline').innerHTML = g;
})();

(function candidates(){
  const groups = {};
  D.candidates.forEach(c => (groups[c.group] ||= []).push(c));
  const keys = Object.keys(groups).sort((a,b)=>(groups[a][0].ruled_out?1:0)-(groups[b][0].ruled_out?1:0));
  document.getElementById('candidates').innerHTML = keys.map(k=>{
    const live = !groups[k][0].ruled_out;
    return `<div class="band">${live?'Still possible':'Ruled out'} &mdash; ${k}</div>
      <div class="cards">` + groups[k].map(c=>`
        <div class="card${c.ruled_out?' out':''}">
          <div class="card-head"><h3>${c.title}</h3>
            <span class="tag ${c.ruled_out?'out':'live'}">${c.ruled_out?'ruled out':'live'}</span></div>
          <p>${c.body}</p>
        </div>`).join('') + `</div>`;
  }).join('');
})();

(function retries(){
  const max = Math.max(...D.retries.flatMap(r=>[r.retry_done,r.no_retry_done]));
  document.getElementById('retries').innerHTML = D.retries.map(r=>`
    <div class="cmp-row">
      <div class="cmp-name">${r.title}<em>retrying completes ${Math.round(r.gain*100)}% more work</em></div>
      <div class="bars">
        <div class="bar-line"><div class="bar-track"><div class="bar-fill" style="width:${r.no_retry_done/max*100}%;background:var(--muted)"></div></div><span>${(r.no_retry_done/1000).toFixed(1)}k without retries</span></div>
        <div class="bar-line"><div class="bar-track"><div class="bar-fill" style="width:${r.retry_done/max*100}%;background:var(--ok)"></div></div><span>${(r.retry_done/1000).toFixed(1)}k with retries</span></div>
      </div>
    </div>`).join('');
})();

document.getElementById('waitTable').innerHTML =
  '<thead><tr><th>Attempts per call</th><th>Longest wait reachable</th><th></th></tr></thead><tbody>' +
  D.wait_by_attempts.map(w=>{
    const hit = w.slowest>=25 && w.slowest<=60;
    const note = w.attempts<=3 ? "the client library's own default" : (hit?'matches what we actually see':'');
    return `<tr class="${hit?'hit':''}"><td>${w.attempts}</td><td>${w.slowest}s</td><td>${note}</td></tr>`;
  }).join('') + '</tbody>';
</script>
"""

out = pathlib.Path("artifact/why-our-agents-are-slow.html")
out.write_text(HTML.replace("__DATA__", json.dumps(D, separators=(",", ":"))))
print("wrote", out, out.stat().st_size, "bytes")
