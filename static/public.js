const $ = (selector) => document.querySelector(selector);

const SYSTEM_ORDER = [
  'A_CONVENTIONAL_RAG',
  'B_BASIC_TIME_FILTERED_RAG',
  'C_AMENDMENT_AWARE_TEMPORAL_RAG'
];

const SYSTEM_CONFIG = {
  A_CONVENTIONAL_RAG: {
    badge: 'System A',
    pillClass: 'pill-a',
    cardClass: 'sys-a',
    title: 'Conventional RAG',
    subtag: 'Baseline · Standard unconstrained retrieval'
  },
  B_BASIC_TIME_FILTERED_RAG: {
    badge: 'System B',
    pillClass: 'pill-b',
    cardClass: 'sys-b',
    title: 'Basic Time-Filtered RAG',
    subtag: 'Basic temporal filter on legal interval'
  },
  C_AMENDMENT_AWARE_TEMPORAL_RAG: {
    badge: 'System C ★',
    pillClass: 'pill-c',
    cardClass: 'sys-c',
    title: 'Amendment-Aware Temporal RAG',
    subtag: 'Proposed model · Temporal & amendment aware'
  }
};

const STEPS = ['Validate question', 'Check research corpus', 'Retrieve legal evidence', 'Check temporal metadata', 'Prepare response', 'Display results'];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function formatMarkdown(text) {
  if (!text) return '';
  
  // First escape HTML entities for safety
  let raw = escapeHtml(text);

  // Bold: **content** -> <strong>content</strong>
  raw = raw.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');

  // Italics: *content* -> <em>content</em>
  raw = raw.replace(/(^|[^\*])\*([^\*]+?)\*([^\*]|$)/g, '$1<em>$2</em>$3');

  // Highlight Source tags: [Source 1], [Source X]
  raw = raw.replace(/\[(Source\s*\d+)\]/gi, '<span class="source-ref-pill">$1</span>');

  // Split lines to format paragraphs, headers, lists, and banners
  const lines = raw.split('\n');
  let inList = false;
  let listType = '';
  const htmlParts = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) {
      if (inList) {
        htmlParts.push(`</${listType}>`);
        inList = false;
      }
      continue;
    }

    // Check for INSUFFICIENT EVIDENCE header
    if (line === 'INSUFFICIENT EVIDENCE' || line.startsWith('INSUFFICIENT EVIDENCE')) {
      if (inList) { htmlParts.push(`</${listType}>`); inList = false; }
      htmlParts.push(`<div class="insufficient-banner"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg> INSUFFICIENT STATUTORY EVIDENCE</div>`);
      const rest = line.replace(/^INSUFFICIENT EVIDENCE[:\s-]*/, '').trim();
      if (rest) htmlParts.push(`<p class="answer-p">${rest}</p>`);
      continue;
    }

    // Section subheadings (e.g. ### Title or Key points:)
    if (line.startsWith('### ')) {
      if (inList) { htmlParts.push(`</${listType}>`); inList = false; }
      htmlParts.push(`<h4>${line.slice(4)}</h4>`);
      continue;
    }
    if (/^(Key points|Key Points|Statutory Analysis|Conclusion):/i.test(line)) {
      if (inList) { htmlParts.push(`</${listType}>`); inList = false; }
      htmlParts.push(`<div class="answer-section-title">${line}</div>`);
      continue;
    }

    // Numbered lists: 1. Item
    const olMatch = line.match(/^(\d+)\.\s+(.+)$/);
    if (olMatch) {
      if (!inList || listType !== 'ol') {
        if (inList) htmlParts.push(`</${listType}>`);
        htmlParts.push('<ol class="answer-ol">');
        inList = true;
        listType = 'ol';
      }
      htmlParts.push(`<li>${olMatch[2]}</li>`);
      continue;
    }

    // Bullet lists: - Item or * Item
    const ulMatch = line.match(/^[-*•]\s+(.+)$/);
    if (ulMatch) {
      if (!inList || listType !== 'ul') {
        if (inList) htmlParts.push(`</${listType}>`);
        htmlParts.push('<ul class="answer-ul">');
        inList = true;
        listType = 'ul';
      }
      htmlParts.push(`<li>${ulMatch[1]}</li>`);
      continue;
    }

    // Normal paragraph
    if (inList) {
      htmlParts.push(`</${listType}>`);
      inList = false;
    }
    htmlParts.push(`<p class="answer-p">${line}</p>`);
  }

  if (inList) {
    htmlParts.push(`</${listType}>`);
  }

  return htmlParts.join('');
}

function updateProgress(event) {
  $('#progressTitle').textContent = event.label || 'Processing';
  $('#progressPercent').textContent = `${event.percent || 0}%`;
  $('#progressBar').style.width = `${event.percent || 0}%`;
  $('#progressDetail').textContent = event.detail || '';
  
  const liveTitle = $('#liveStatusTitle');
  const liveDetail = $('#liveStatusDetail');
  if (liveTitle) liveTitle.textContent = event.label || 'Analyzing Legal Evidence';
  if (liveDetail) liveDetail.textContent = event.detail || 'Cross-referencing statutory intervals and IRD rules...';

  document.querySelectorAll('.progress-step').forEach(el => {
    const n = Number(el.dataset.step);
    el.classList.toggle('done', n < (event.step || 0));
    el.classList.toggle('active', n === (event.step || 0));
    const indicator = el.querySelector('.step-indicator');
    if (indicator) indicator.textContent = n < (event.step || 0) ? '✓' : String(n);
  });
}

function showProgress() {
  $('#progressPanel').hidden = false;
  $('#resultsPanel').hidden = true;
  $('#progressTitle').textContent = 'Working on your request';
  $('#progressPercent').textContent = '0%';
  $('#progressBar').style.width = '0%';
  $('#progressDetail').textContent = 'Starting…';
  const liveWidget = document.querySelector('.progress-live-animation');
  if (liveWidget) liveWidget.style.display = 'flex';
  const liveTitle = $('#liveStatusTitle');
  const liveDetail = $('#liveStatusDetail');
  if (liveTitle) liveTitle.textContent = 'Initializing AI Research Engines';
  if (liveDetail) liveDetail.textContent = 'Searching statutory corpus and amendment registers...';
  $('#progressSteps').innerHTML = STEPS.map((label, i) => `<div class="progress-step" data-step="${i + 1}"><span class="step-indicator">${i + 1}</span><span>${escapeHtml(label)}</span></div>`).join('');
}

async function readEvents(response, onEvent) {
  if (!response.ok) {
    const body = await response.text();
    let parsed = {};
    try { parsed = JSON.parse(body); } catch (_) {}
    throw new Error(parsed.error || `Request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finished = false;

  function dispatch(chunk) {
    const lines = chunk.split('\n');
    for (const line of lines) {
      if (line.startsWith('data:')) {
        try {
          const parsed = JSON.parse(line.slice(5).trim());
          onEvent(parsed);
          if (parsed.type === 'result' || parsed.type === 'error') {
            finished = true;
          }
        } catch (err) {
          console.warn('Could not parse progress event', err);
        }
      }
    }
  }

  try {
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf('\n\n')) >= 0) {
        dispatch(buffer.slice(0, boundary));
        buffer = buffer.slice(boundary + 2);
        if (finished) break;
      }
    }
    if (!finished && buffer.trim()) {
      dispatch(buffer);
    }
  } finally {
    try { await reader.cancel(); } catch (_) {}
  }
}

function renderEvidence(evidence) {
  if (!evidence.length) return '<div class="no-evidence">No statutory evidence records retrieved for this system.</div>';
  return `<div class="source-list">${evidence.map((source, index) => {
    const title = source.title || source.source_document || 'Legal source';
    const valid = [source.valid_time_start, source.valid_time_end].filter(Boolean).join(' – ') || 'Validity interval not verified';
    const excerpt = source.chunk_text || source.retrieval_context || source.retrieval_text || '';
    return `
      <article class="source-item">
        <span class="source-chip">Source ${index + 1}</span>
        <div class="source-title">${escapeHtml(title)}</div>
        <div class="source-sub">${escapeHtml(source.source_document || source.record_type || 'Research corpus record')}</div>
        <div class="source-sub">Applicable period: ${escapeHtml(valid)}</div>
        ${excerpt ? `<details><summary>View statutory passage</summary><div class="source-passage">${escapeHtml(excerpt)}</div></details>` : ''}
      </article>
    `;
  }).join('')}</div>`;
}

function renderResult(result) {
  try {
    $('#resultsPanel').hidden = false;
    $('#progressPanel').hidden = true;
    $('#resultNotice').textContent = result.notice || '';
    $('#resultNotice').className = 'result-notice' + (result.mode === 'local_evidence_preview' ? ' warning' : '');
    if (result.mode === 'local_evidence_preview') {
      $('#resultNotice').textContent = `${result.notice || ''} This is an evidence-only preview, not an AI-generated tax answer.`;
    }

    const rawSystems = result.systems || [];
    // Ensure the systems are ALWAYS ordered strictly A -> B -> C
    const systems = [...rawSystems].sort((a, b) => {
      const ia = SYSTEM_ORDER.indexOf(a.system_id);
      const ib = SYSTEM_ORDER.indexOf(b.system_id);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });

    const resultsGrid = $('#resultsContent');
    resultsGrid.className = 'results-grid' + (systems.length ? ` count-${systems.length}` : '');

    resultsGrid.innerHTML = systems.map(system => {
      const cfg = SYSTEM_CONFIG[system.system_id] || {
        badge: system.system_id || 'System',
        pillClass: 'pill-a',
        cardClass: 'sys-a',
        title: system.system_name || 'RAG System',
        subtag: 'Tax research system'
      };
      const status = system.archived
        ? 'Saved controlled-run output'
        : (result.mode === 'live_backend' ? 'Live generated response' : 'Evidence preview only');

      const answer = system.answer
        ? formatMarkdown(system.answer)
        : '<p class="answer-p muted">No generated answer is available for this question. Review the retrieved evidence below.</p>';

      const evidenceList = system.evidence || [];

      return `
        <article class="system-card ${cfg.cardClass}">
          <header class="system-card-head">
            <div class="system-badge-row">
              <span class="system-pill ${cfg.pillClass}">${escapeHtml(cfg.badge)}</span>
              <span class="answer-status">${escapeHtml(status)}</span>
            </div>
            <h3>${escapeHtml(cfg.title)}</h3>
            <div class="system-subtag">${escapeHtml(cfg.subtag)}</div>
          </header>
          <div class="system-body">
            <div class="system-answer-label">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
              LEGAL ANSWER
            </div>
            <div class="system-answer">${answer}</div>
          </div>
          <div class="system-meta">
            <span>${system.archived ? 'Archived experiment' : 'Runtime inference'}</span>
            <span>${evidenceList.length} source record${evidenceList.length === 1 ? '' : 's'}</span>
          </div>
          ${renderEvidence(evidenceList)}
        </article>
      `;
    }).join('');

    $('#resultsPanel').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (err) {
    console.error('Error rendering result:', err);
    $('#progressTitle').textContent = 'Error displaying results';
    $('#progressDetail').textContent = err.message || 'An unexpected rendering error occurred.';
  }
}

$('#queryForm').addEventListener('submit', async event => {
  event.preventDefault();
  const question = $('#questionInput').value.trim();
  if (question.length < 8) {
    $('#questionInput').focus();
    return;
  }
  const button = $('#submitQuery');
  button.disabled = true;
  button.querySelector('span').textContent = 'Searching…';
  showProgress();
  try {
    const response = await fetch('/api/query/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question,
        legal_date: $('#legalDate').value,
        mode: $('#systemMode').value
      })
    });
    let result = null;
    let streamError = '';
    await readEvents(response, data => {
      if (data.type === 'progress') updateProgress(data);
      if (data.type === 'result') {
        result = data.result;
        renderResult(result);
      }
      if (data.type === 'error') streamError = data.message || 'The request failed.';
    });
    if (streamError) throw new Error(streamError);
    if (!result) throw new Error('No result was returned.');
  } catch (error) {
    console.error('Search request failed:', error);
    $('#progressTitle').textContent = 'Request could not be completed';
    $('#progressPercent').textContent = '⚠️';
    $('#progressDetail').innerHTML = `
      <div style="color: #b91c1c; font-weight: 600; margin-bottom: 8px;">
        ${escapeHtml(error.message || 'The network connection was interrupted or timed out.')}
      </div>
      <button type="button" class="secondary-button" onclick="$('#queryForm').requestSubmit()" style="margin-top: 6px;">
        ↻ Retry search
      </button>
    `;
    const liveWidget = document.querySelector('.progress-live-animation');
    if (liveWidget) liveWidget.style.display = 'none';
  } finally {
    button.disabled = false;
    button.querySelector('span').textContent = 'Search legal sources';
  }
});

$('#clearResults').addEventListener('click', () => {
  $('#resultsPanel').hidden = true;
  $('#progressPanel').hidden = true;
  $('#resultsContent').innerHTML = '';
  $('#questionInput').value = '';
  document.querySelectorAll('.suggestion-chip').forEach(c => c.classList.remove('active'));
  $('#questionInput').focus();
  window.scrollTo({ top: 0, behavior: 'smooth' });
});

document.querySelectorAll('.suggestion-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const question = chip.getAttribute('data-question');
    const date = chip.getAttribute('data-date');
    if (question) {
      $('#questionInput').value = question;
      if (date && $('#legalDate')) {
        $('#legalDate').value = date;
      }
      document.querySelectorAll('.suggestion-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      $('#questionInput').focus();
      // Smooth visual feedback
      $('#questionInput').style.borderColor = 'var(--blue2)';
      setTimeout(() => {
        $('#questionInput').style.borderColor = '';
      }, 700);
    }
  });
});
