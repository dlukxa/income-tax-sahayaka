from __future__ import annotations
import csv, io, json, os, re, sqlite3, zipfile, hashlib, secrets, time
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, session, Response, send_file, send_from_directory, abort

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / '.env'
if ENV_FILE.is_file():
    try:
        with open(ENV_FILE, encoding='utf-8') as _ef:
            for _line in _ef:
                _line = _line.strip()
                if _line and not _line.startswith('#') and '=' in _line:
                    _k, _v = _line.split('=', 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception:
        pass
DATA_DIR = BASE_DIR / 'data'
SEED_PATH = DATA_DIR / 'seed_data.zip'

# Serverless read-only filesystem support (Vercel, AWS Lambda, etc.)
if os.environ.get('VERCEL') == '1' or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
    INSTANCE_DIR = Path('/tmp')
    DB_PATH = Path(os.environ.get('TAX_RESEARCH_DB', '/tmp/research_app.sqlite3'))
    bundled_db = BASE_DIR / 'instance' / 'research_app.sqlite3'
    if bundled_db.exists() and not DB_PATH.exists():
        try:
            import shutil
            shutil.copy2(bundled_db, DB_PATH)
        except Exception:
            pass
else:
    INSTANCE_DIR = BASE_DIR / 'instance'
    INSTANCE_DIR.mkdir(exist_ok=True)
    DB_PATH = Path(os.environ.get('TAX_RESEARCH_DB', INSTANCE_DIR / 'research_app.sqlite3'))

app = Flask(__name__, template_folder=str(BASE_DIR / 'templates'), static_folder=str(BASE_DIR / 'static'))
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'local-prototype-secret-change-before-deployment')
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
app.config['JSON_SORT_KEYS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '0') == '1'

SYSTEM_NAMES = {
    'A_CONVENTIONAL_RAG': 'Conventional RAG',
    'B_BASIC_TIME_FILTERED_RAG': 'Basic Time-Filtered RAG',
    'C_AMENDMENT_AWARE_TEMPORAL_RAG': 'Amendment-Aware Temporal RAG',
}
CODE_TO_SYSTEM = {'SYSTEM_X': 'A_CONVENTIONAL_RAG', 'SYSTEM_Y': 'B_BASIC_TIME_FILTERED_RAG', 'SYSTEM_Z': 'C_AMENDMENT_AWARE_TEMPORAL_RAG'}
SYSTEM_TO_CODE = {v: k for k, v in CODE_TO_SYSTEM.items()}
RUN_ID = '20261027T092018Z'

ANSWER_REVIEW_FIELDS = {
    'substantive_correctness': ['CORRECT', 'PARTIALLY_CORRECT', 'INCORRECT', 'UNCLEAR'],
    'temporal_version_correctness': ['CORRECT', 'INCORRECT', 'UNCLEAR', 'NOT_APPLICABLE'],
    'answer_completeness_conditions': ['COMPLETE', 'PARTIALLY_COMPLETE', 'INCOMPLETE', 'NOT_APPLICABLE'],
    'section_subsection_correctness': ['YES', 'NO', 'UNCLEAR', 'NOT_APPLICABLE'],
    'citation_target_correctness': ['YES', 'NO', 'UNCLEAR', 'NO_CITATION', 'NOT_APPLICABLE'],
    'citation_entailment': ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED', 'NO_CITATION', 'NOT_APPLICABLE'],
    'actual_context_support': ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED', 'UNCLEAR'],
    'unsupported_claims_present': ['YES', 'NO', 'UNCLEAR'],
    'answer_or_abstention_appropriate': ['APPROPRIATE_ANSWER', 'SHOULD_ABSTAIN', 'SHOULD_ANSWER', 'UNCLEAR'],
    'answer_relevance': ['1', '2', '3', '4', '5'],
    'nugget_coverage_pct': 'number_0_100',
    'citation_present': ['YES', 'NO'],
    'citation_precision': 'number_0_1',
    'citation_recall': 'number_0_1',
    'numeric_values_correct': ['YES', 'NO', 'UNCLEAR', 'NA'],
    'contradiction_free': ['YES', 'NO', 'UNCLEAR'],
    'source_reference_checked': ['YES', 'NO'],
    'valid_time_checked': ['YES', 'NO'],
    'review_notes': 'text'
}
RETRIEVAL_FIELDS = {
    'record_is_correct_statutory_source': ['CORRECT', 'INCORRECT', 'UNCLEAR'],
    'record_version_valid_for_target_period': ['VALID', 'INVALID', 'UNCLEAR', 'NOT_APPLICABLE'],
    'future_amendment_leakage': ['YES', 'NO', 'UNCLEAR'],
    'source_clause_checked': ['YES', 'NO'],
    'review_notes': 'text'
}
PROMPT_FIELDS = {
    'human_factual_correctness_1to5': ['1', '2', '3', '4', '5'],
    'human_faithfulness_1to5': ['1', '2', '3', '4', '5'],
    'human_completeness_1to5': ['1', '2', '3', '4', '5'],
    'human_temporal_appropriateness_1to5': ['1', '2', '3', '4', '5'],
    'human_citation_accuracy_1to5': ['1', '2', '3', '4', '5'],
    'reviewer_notes': 'text'
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def table_exists(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def init_db():
    with get_db() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS questions (
            question_id TEXT PRIMARY KEY, question_en TEXT NOT NULL, target_tax_period TEXT DEFAULT '',
            target_as_of_date TEXT DEFAULT '', valid_time_start TEXT DEFAULT '', valid_time_end TEXT DEFAULT '',
            approved_gold_record_ids TEXT DEFAULT '', approved_statutory_citation TEXT DEFAULT '',
            approved_reference_answer_en TEXT DEFAULT '', gold_status TEXT DEFAULT 'PENDING',
            gold_review_notes TEXT DEFAULT '', approved_by TEXT DEFAULT '', approved_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS answers (
            question_id TEXT NOT NULL, system_id TEXT NOT NULL, system_name TEXT DEFAULT '',
            question_en TEXT DEFAULT '', generated_answer TEXT DEFAULT '', generation_status TEXT DEFAULT '',
            top5_parent_ids TEXT DEFAULT '', allowed_record_ids TEXT DEFAULT '', selected_context_chunk_ids TEXT DEFAULT '',
            actual_context_text TEXT DEFAULT '', context_sha256 TEXT DEFAULT '', prompt_sha256 TEXT DEFAULT '',
            prompt_template_version TEXT DEFAULT '', latency_seconds REAL DEFAULT 0,
            posthoc_qa_flags TEXT DEFAULT '', posthoc_qa_flag_count INTEGER DEFAULT 0,
            PRIMARY KEY(question_id, system_id)
        );
        CREATE TABLE IF NOT EXISTS chunks (
            record_id TEXT NOT NULL, chunk_id TEXT DEFAULT '', record_type TEXT DEFAULT '', title TEXT DEFAULT '',
            section_number TEXT DEFAULT '', target TEXT DEFAULT '', act_section TEXT DEFAULT '',
            valid_time_start TEXT DEFAULT '', valid_time_end TEXT DEFAULT '', knowledge_time_start TEXT DEFAULT '',
            certified_date TEXT DEFAULT '', publication_date TEXT DEFAULT '', snapshot_date TEXT DEFAULT '',
            source_document TEXT DEFAULT '', source_page_start TEXT DEFAULT '', source_page_end TEXT DEFAULT '',
            source_text_scope TEXT DEFAULT '', record_status TEXT DEFAULT '', review_flag TEXT DEFAULT '',
            retrieval_context TEXT DEFAULT '', related_parent_register_ids TEXT DEFAULT '', operation_review_status TEXT DEFAULT '',
            approved_for_merge TEXT DEFAULT '', date_alignment_status TEXT DEFAULT '', research_freeze_status TEXT DEFAULT '',
            retrieval_use_policy TEXT DEFAULT '', chunk_text TEXT DEFAULT '', retrieval_text TEXT DEFAULT '',
            PRIMARY KEY(record_id, chunk_id)
        );
        CREATE TABLE IF NOT EXISTS retrieval_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT NOT NULL, review_system_code TEXT NOT NULL,
            system_id TEXT NOT NULL, question_en TEXT DEFAULT '', parent_rank INTEGER DEFAULT 0, parent_record_id TEXT DEFAULT '',
            adjusted_score REAL DEFAULT 0, base_hybrid_score REAL DEFAULT 0, candidate_interval_state TEXT DEFAULT '',
            ranking_reason TEXT DEFAULT '', query_date_used TEXT DEFAULT '', date_source TEXT DEFAULT '',
            query_scope_type TEXT DEFAULT '', record_type TEXT DEFAULT '', title TEXT DEFAULT '',
            valid_time_start TEXT DEFAULT '', valid_time_end TEXT DEFAULT '', source_document TEXT DEFAULT '',
            review_flag TEXT DEFAULT '', retrieval_mode TEXT DEFAULT '', target_tax_period_candidate TEXT DEFAULT '',
            UNIQUE(question_id, system_id, parent_rank)
        );
        CREATE TABLE IF NOT EXISTS answer_review_tasks (
            review_row_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, review_system_code TEXT NOT NULL,
            reviewer_slot INTEGER NOT NULL, reviewer_id TEXT DEFAULT '', status TEXT DEFAULT 'PENDING',
            payload_json TEXT DEFAULT '{}', reviewed_at TEXT DEFAULT '',
            FOREIGN KEY(question_id) REFERENCES questions(question_id)
        );
        CREATE TABLE IF NOT EXISTS answer_adjudications (
            question_id TEXT NOT NULL, review_system_code TEXT NOT NULL, system_id TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING', adjudicator_id TEXT DEFAULT '', adjudication_date TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}', PRIMARY KEY(question_id, review_system_code)
        );
        CREATE TABLE IF NOT EXISTS retrieval_review_submissions (
            item_id INTEGER NOT NULL, reviewer_slot INTEGER NOT NULL, reviewer_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY(item_id, reviewer_slot),
            FOREIGN KEY(item_id) REFERENCES retrieval_items(item_id)
        );
        CREATE TABLE IF NOT EXISTS retrieval_adjudications (
            item_id INTEGER PRIMARY KEY, final_adjudicator_id TEXT DEFAULT '', final_label TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}', adjudicated_at TEXT DEFAULT '',
            FOREIGN KEY(item_id) REFERENCES retrieval_items(item_id)
        );
        CREATE TABLE IF NOT EXISTS gold_review_submissions (
            question_id TEXT NOT NULL, reviewer_slot INTEGER NOT NULL, reviewer_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY(question_id, reviewer_slot),
            FOREIGN KEY(question_id) REFERENCES questions(question_id)
        );
        CREATE TABLE IF NOT EXISTS prompt_review_items (
            task_id INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT NOT NULL, strategy_key TEXT NOT NULL,
            strategy_name TEXT DEFAULT '', question_en TEXT DEFAULT '', question_si TEXT DEFAULT '',
            expected_answer_en TEXT DEFAULT '', expected_answer_si TEXT DEFAULT '', expected_answer_status TEXT DEFAULT '',
            source_reference TEXT DEFAULT '', top5_parent_ids TEXT DEFAULT '', generated_answer TEXT DEFAULT '',
            cited_record_ids TEXT DEFAULT '', invalid_cited_record_ids TEXT DEFAULT '',
            rouge_proxy REAL, citation_validity_rate REAL, bilingual_response TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}', review_status TEXT DEFAULT 'PENDING', reviewer_id TEXT DEFAULT '', reviewed_at TEXT DEFAULT '',
            UNIQUE(question_id, strategy_key)
        );
        CREATE TABLE IF NOT EXISTS query_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, question TEXT NOT NULL,
            legal_date TEXT DEFAULT '', mode TEXT DEFAULT '', result_mode TEXT DEFAULT '', success INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT DEFAULT '');
        ''')
        count = db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
        if count == 0:
            seed_database(db)


def csv_rows(zf, name):
    with zf.open(name) as f:
        txt = io.TextIOWrapper(f, encoding='utf-8-sig', newline='')
        return list(csv.DictReader(txt))


def seed_database(db):
    if not SEED_PATH.exists():
        raise RuntimeError(f'Seed file missing: {SEED_PATH}')
    with zipfile.ZipFile(SEED_PATH) as z:
        manifest = json.loads(z.read('run_manifest.json').decode('utf-8'))
        e05_manifest = json.loads(z.read('prompt_run_manifest.json').decode('utf-8'))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('run_manifest',?)", (json.dumps(manifest),))
        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('prompt_manifest',?)", (json.dumps(e05_manifest),))
        # Server-side mapping only. It is never sent to the blind-review interface or included in the data seed.
        for code, system_id in CODE_TO_SYSTEM.items():
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", ('private_code_'+code, system_id))
        for r in csv_rows(z, 'questions_gold_template.csv'):
            db.execute('''INSERT OR IGNORE INTO questions(question_id,question_en,target_tax_period,target_as_of_date,valid_time_start,valid_time_end,approved_gold_record_ids,approved_statutory_citation,approved_reference_answer_en,gold_status,gold_review_notes)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (r.get('question_id',''),r.get('question_en',''),r.get('target_tax_period',''),r.get('target_as_of_date',''),r.get('valid_time_start',''),r.get('valid_time_end',''),r.get('approved_gold_record_ids',''),r.get('approved_statutory_citation',''),r.get('approved_reference_answer_en',''),'PENDING',r.get('review_notes','')))
        for r in csv_rows(z, 'answers_600.csv'):
            db.execute('''INSERT OR IGNORE INTO answers(question_id,system_id,system_name,question_en,generated_answer,generation_status,top5_parent_ids,allowed_record_ids,selected_context_chunk_ids,actual_context_text,context_sha256,prompt_sha256,prompt_template_version,latency_seconds,posthoc_qa_flags,posthoc_qa_flag_count)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (r.get('question_id',''),r.get('system_id',''),r.get('system_name',''),r.get('question_en',''),r.get('generated_answer',''),r.get('generation_status',''),r.get('top5_parent_ids',''),r.get('allowed_record_ids',''),r.get('selected_context_chunk_ids',''),r.get('actual_context_text',''),r.get('context_sha256',''),r.get('prompt_sha256',''),r.get('prompt_template_version',''),float(r.get('latency_seconds') or 0),r.get('posthoc_qa_flags',''),int(r.get('posthoc_qa_flag_count') or 0)))
        # runtime chunks
        for r in csv_rows(z,'runtime_chunks_363.csv'):
            db.execute('''INSERT OR IGNORE INTO chunks(record_id,chunk_id,record_type,title,section_number,target,act_section,valid_time_start,valid_time_end,knowledge_time_start,certified_date,publication_date,snapshot_date,source_document,source_page_start,source_page_end,source_text_scope,record_status,review_flag,retrieval_context,related_parent_register_ids,operation_review_status,approved_for_merge,date_alignment_status,research_freeze_status,retrieval_use_policy,chunk_text,retrieval_text)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (r.get('record_id',''),r.get('chunk_id',''),r.get('record_type',''),r.get('title',''),r.get('section_number',''),r.get('target',''),r.get('act_section',''),r.get('valid_time_start',''),r.get('valid_time_end',''),r.get('knowledge_time_start',''),r.get('certified_date',''),r.get('publication_date',''),r.get('snapshot_date',''),r.get('source_document',''),r.get('source_page_start',''),r.get('source_page_end',''),r.get('source_text_scope',''),r.get('record_status',''),r.get('review_flag',''),r.get('retrieval_context',''),r.get('related_parent_register_ids_v1648',''),r.get('operation_review_status_v1648',''),r.get('approved_for_merge_v1648',''),r.get('date_alignment_status_v1648',''),r.get('research_freeze_status_v1648',''),r.get('retrieval_use_policy_v1648',''),r.get('chunk_text',''),r.get('retrieval_text','')))
        # retrieval review records; blind code map hidden from UI
        periods = {r['question_id']: r.get('target_tax_period','') for r in csv_rows(z,'questions_gold_template.csv')}
        for r in csv_rows(z,'retrieval_top5_3000.csv'):
            sysid=r['system_id']; code=SYSTEM_TO_CODE.get(sysid,'SYSTEM_X')
            db.execute('''INSERT OR IGNORE INTO retrieval_items(question_id,review_system_code,system_id,question_en,parent_rank,parent_record_id,adjusted_score,base_hybrid_score,candidate_interval_state,ranking_reason,query_date_used,date_source,query_scope_type,record_type,title,valid_time_start,valid_time_end,source_document,review_flag,retrieval_mode,target_tax_period_candidate)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (r.get('question_id',''),code,sysid,r.get('question_id',''),int(r.get('parent_rank') or 0),r.get('parent_record_id',''),float(r.get('adjusted_score') or 0),float(r.get('base_hybrid_score') or 0),r.get('candidate_interval_state',''),r.get('ranking_reason',''),r.get('query_date_used',''),r.get('date_source',''),r.get('query_scope_type',''),r.get('record_type',''),r.get('title',''),r.get('valid_time_start',''),r.get('valid_time_end',''),r.get('source_document',''),r.get('review_flag',''),r.get('retrieval_mode',''),periods.get(r.get('question_id',''),'')))
        db.execute("UPDATE retrieval_items SET question_en=(SELECT question_en FROM questions WHERE questions.question_id=retrieval_items.question_id)")
        # answer reviews; no answer/context stored duplicate, joined privately to answer rows
        for r in csv_rows(z,'answer_review_template_1200.csv'):
            slot=1 if 'REVIEWER_1' in r.get('reviewer_slot','') else 2
            review_id=r.get('review_row_id') or f"{r.get('question_id')}__{r.get('review_system_code')}__REVIEWER_{slot}"
            db.execute('''INSERT OR IGNORE INTO answer_review_tasks(review_row_id,question_id,review_system_code,reviewer_slot,reviewer_id,status,payload_json,reviewed_at) VALUES(?,?,?,?,?,?,?,?)''', (review_id,r.get('question_id',''),r.get('review_system_code','SYSTEM_X'),slot,'','PENDING','{}',''))
        # All adjudication tasks (blind review code, internal mapping kept server-side)
        for r in csv_rows(z,'answer_adjudication_template_600.csv'):
            code=r.get('review_system_code') or SYSTEM_TO_CODE.get(r.get('system_id',''),'SYSTEM_X')
            sid=r.get('system_id') or CODE_TO_SYSTEM.get(code,'A_CONVENTIONAL_RAG')
            db.execute('''INSERT OR IGNORE INTO answer_adjudications(question_id,review_system_code,system_id,status) VALUES(?,?,?,'PENDING')''',(r.get('question_id',''),code,sid))
        # E05 prompt review tasks
        for r in csv_rows(z,'prompt_review_template_350.csv'):
            payload={k:r.get(k,'') for k in ['mean_reference_rougeL_recall_proxy','citation_validity_rate','bilingual_response']}
            db.execute('''INSERT OR IGNORE INTO prompt_review_items(question_id,strategy_key,strategy_name,question_en,question_si,expected_answer_en,expected_answer_si,expected_answer_status,source_reference,top5_parent_ids,generated_answer,cited_record_ids,invalid_cited_record_ids,rouge_proxy,citation_validity_rate,bilingual_response,payload_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (r.get('question_id',''),r.get('prompt_strategy_key',''),r.get('prompt_strategy_name',''),r.get('question_en',''),r.get('question_si',''),r.get('expected_answer_en',''),r.get('expected_answer_si',''),r.get('expected_answer_status',''),r.get('source_reference',''),r.get('top5_parent_ids',''),r.get('generated_answer',''),r.get('cited_record_ids',''),r.get('invalid_cited_record_ids',''),float(r.get('mean_reference_rougeL_recall_proxy') or 0),float(r.get('citation_validity_rate') or 0),r.get('bilingual_response',''),json.dumps(payload)))
    db.commit()


def reviewer_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('reviewer_id'):
            if request.path.startswith('/api/'):
                return jsonify({'error':'Please sign in to the review workspace.'}), 401
            return abort(401)
        if session.get('role') == 'tax_expert':
            return jsonify({'error':'The tax-expert account can access only its legal-validation form.'}), 403
        return f(*args, **kwargs)
    return wrapper


def tax_expert_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('reviewer_id'):
            return jsonify({'error':'Please sign in to the tax-expert form.'}), 401
        if session.get('role') != 'tax_expert':
            return jsonify({'error':'This endpoint is reserved for tax-expert accounts.'}), 403
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'admin':
            if request.path.startswith('/api/'):
                return jsonify({'error':'Administrator access is required for this action.'}), 403
            return abort(403)
        return f(*args, **kwargs)
    return wrapper


def norm_text(s):
    return re.sub(r'\s+', ' ', re.sub(r'[^a-z0-9 ]+', ' ', (s or '').lower())).strip()


def tokenise(s):
    return re.findall(r'[a-z0-9]+', (s or '').lower())


def bm25_rank(query, docs, limit=5):
    qtokens = set(tokenise(query))
    if not qtokens: return []
    N=len(docs)
    toks=[tokenise(d.get('search_text','')) for d in docs]
    avg_len=(sum(map(len,toks))/N) if N else 0
    dfs={t:sum(1 for ts in toks if t in set(ts)) for t in qtokens}
    scored=[]
    for d,ts in zip(docs,toks):
        if not ts: continue
        freq={}
        for t in ts: freq[t]=freq.get(t,0)+1
        score=0.0
        dl=len(ts)
        for term in qtokens:
            tf=freq.get(term,0)
            if not tf: continue
            df=dfs.get(term,0)
            idf=max(0.0, __import__('math').log(1+(N-df+0.5)/(df+0.5)))
            score += idf * (tf*2.2)/(tf+1.2*(0.25+0.75*(dl/(avg_len or 1))))
        if score>0:
            item=dict(d); item['score']=score
            scored.append(item)
    scored.sort(key=lambda x:x['score'],reverse=True)
    # deduplicate parent records while preserving best chunk
    seen=set(); out=[]
    for d in scored:
        rid=d.get('record_id') or d.get('parent_record_id')
        if rid in seen: continue
        seen.add(rid); out.append(d)
        if len(out)>=limit: break
    return out


def make_sse(payload):
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'


def answer_archive_for_question(db, question):
    target=norm_text(question)
    if not target: return None
    # exact normalized match first; do not guess using fuzzy matches for legal answers
    row=db.execute('SELECT question_id,question_en FROM questions').fetchall()
    hit=next((r for r in row if norm_text(r['question_en']) == target),None)
    if not hit: return None
    answers=db.execute('SELECT * FROM answers WHERE question_id=? ORDER BY system_id',(hit['question_id'],)).fetchall()
    qrow=db.execute('SELECT * FROM questions WHERE question_id=?',(hit['question_id'],)).fetchone()
    result=[]
    for a in answers:
        top=db.execute('SELECT * FROM retrieval_items WHERE question_id=? AND system_id=? ORDER BY parent_rank',(hit['question_id'],a['system_id'])).fetchall()
        result.append({'system_id':a['system_id'],'system_name':SYSTEM_NAMES.get(a['system_id'],a['system_name']),'answer':a['generated_answer'],'archived':True,'latency_seconds':a['latency_seconds'],'top5_parent_ids':a['top5_parent_ids'],'context_sha256':a['context_sha256'],'evidence':[dict(t) for t in top]})
    return {'question_id':hit['question_id'],'question_en':hit['question_en'],'target_tax_period':qrow['target_tax_period'],'systems':result}


def search_local_corpus(db, question, legal_date, system_id):
    rows=db.execute('SELECT * FROM chunks').fetchall()
    docs=[]
    for row in rows:
        d=dict(row)
        d['search_text']=' '.join([d.get('title',''),d.get('section_number',''),d.get('target',''),d.get('retrieval_text',''),d.get('chunk_text',''),d.get('retrieval_context','')])
        docs.append(d)
    # Normalize spaced numbers like 100 000 -> 100000 and expand employment/salary keywords
    norm_q = re.sub(r'(\d+)\s+(\d{3})', r'\1\2', question or '')
    q_lower = norm_q.lower()
    expanded_terms = []
    if any(k in q_lower for k in ['salary', 'wage', 'employee', 'employment', 'monthly']):
        expanded_terms.extend(['employment income', 'personal relief', 'fifth schedule', 'section 52', 'section 83a'])
    search_q = norm_q + (' ' + ' '.join(expanded_terms) if expanded_terms else '')
    ranked=bm25_rank(search_q,docs,limit=40)
    # Basic temporal filter: exclude records only when their explicit validity interval proves them out of scope.
    filtered=[]
    for d in ranked:
        st=(d.get('valid_time_start') or '').strip(); en=(d.get('valid_time_end') or '').strip()
        if legal_date and system_id in ('B_BASIC_TIME_FILTERED_RAG','C_AMENDMENT_AWARE_TEMPORAL_RAG'):
            if st and legal_date < st: continue
            if en and legal_date > en: continue
        d['temporal_note']='Explicit interval present' if (st or en) else 'No verified explicit validity interval; retained for inspection'
        if system_id=='C_AMENDMENT_AWARE_TEMPORAL_RAG':
            # Ranking hint only, not statutory reconstruction: favour amendment records when question contains an amendment/version clue.
            txt=' '.join([d.get('record_type',''),d.get('title',''),d.get('target',''),d.get('related_parent_register_ids','')]).lower()
            q=question.lower()
            hint_terms=['amendment','amended','version','effective','introduced','repealed','substituted','from when','which year']
            if any(h in q for h in hint_terms) and ('amendment' in txt or 'event' in txt):
                d['score'] *= 1.12
        filtered.append(d)
    filtered.sort(key=lambda x:x['score'],reverse=True)
    # parent records may be separate chunks/duplicate; de-dup
    seen=set(); out=[]
    for d in filtered:
        rid=d.get('record_id')
        if rid in seen: continue
        seen.add(rid); out.append(d)
        if len(out)==5: break
    mx=max([x['score'] for x in out] or [1.0])
    for d in out:
        d['normalized_score']=round(d['score']/mx,4) if mx else 0
    return out


def try_live_backend(question, legal_date, systems, tax_period=''):
    endpoint=os.environ.get('RAG_API_URL','').strip()
    if not endpoint: return None
    # Expected adapter contract documented in README. Never invent an answer if the backend fails.
    import urllib.request
    body=json.dumps({'question':question,'as_of_date':legal_date,'tax_period':tax_period,'systems':systems}).encode('utf-8')
    req=urllib.request.Request(endpoint,data=body,headers={'Content-Type':'application/json'},method='POST')
    timeout=float(os.environ.get('RAG_API_TIMEOUT','180'))
    with urllib.request.urlopen(req,timeout=timeout) as resp:
        data=json.loads(resp.read().decode('utf-8'))
    if not isinstance(data,dict) or not isinstance(data.get('systems'),list):
        raise ValueError('Configured RAG backend response does not match the documented JSON contract.')
    return data


def generate_openrouter_answer(question, legal_date, system_id, evidence, tax_period=''):
    api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not api_key:
        return None
    preferred_model = os.environ.get('OPENROUTER_MODEL', 'qwen/qwen-2.5-7b-instruct').strip()
    
    # Fast, proven models on OpenRouter
    candidate_models = [preferred_model]
    for fallback in ['meta-llama/llama-3.1-8b-instruct', 'qwen/qwen-2.5-7b-instruct']:
        if fallback not in candidate_models:
            candidate_models.append(fallback)
    
    context_parts = []
    for idx, e in enumerate(evidence, 1):
        sec = e.get('section_number') or e.get('target') or ''
        title = e.get('title') or ''
        vt_start = e.get('valid_time_start') or 'unspecified'
        vt_end = e.get('valid_time_end') or 'present'
        txt = e.get('retrieval_text') or e.get('chunk_text') or e.get('retrieval_context') or ''
        context_parts.append(f"[Source {idx}]: {title} (Section {sec})\nEffective Period: {vt_start} to {vt_end}\n{txt.strip()}")
    
    context_str = "\n\n".join(context_parts) if context_parts else "No specific statutory passages retrieved."
    sys_name = SYSTEM_NAMES.get(system_id, system_id)
    
    system_prompt = (
        "You are an expert Sri Lankan tax law research assistant analyzing the Inland Revenue Act, No. 24 of 2017 and its amendments. "
        "Format your response cleanly using bold headers (e.g. **Analysis**, **Tax Liability**, **Statutory Sources**) and bullet points. "
        "Cite specific statutory sections, amendment acts, and effective dates. "
        "For individual salary and employment income queries, clearly explain the Personal Relief threshold under Section 52 and the Fifth Schedule "
        "(Rs. 1,200,000 per year or Rs. 100,000 per month under Act No. 45 of 2022; and Rs. 1,800,000 per year or Rs. 150,000 per month for Y/A 2025/2026 under Act No. 11 of 2025), "
        "and conclude definitively whether a given salary amount (e.g. Rs. 100,000/month) is liable to income tax or covered by relief. "
        "Always distinguish between regular employees (PAYE/APIT under section 83A) and independent service providers (withholding under section 85)."
    )
    
    user_prompt = (
        f"Tax Question: {question}\n"
        f"Applicable Legal Date: {legal_date or 'Current'}\n"
        f"RAG System Approach: {sys_name}\n\n"
        f"Retrieved Statutory Evidence:\n{context_str}\n\n"
        f"Provide a structured, source-grounded legal answer explaining how Sri Lankan tax law applies as of {legal_date or 'the specified period'}. "
        f"Use bold markdown (**Title**) for headings and bullet points. Always cite specific section number(s) and effective dates."
    )
    
    import urllib.request
    import urllib.error
    
    timeout = min(float(os.environ.get('RAG_API_TIMEOUT', '25')), 25.0)
    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://income-tax-sahayaka.vercel.app',
        'X-Title': 'Income Tax Sahayaka'
    }

    last_error = None
    for model in candidate_models:
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt}
            ],
            'temperature': 0.1,
            'max_tokens': 380
        }
        req = urllib.request.Request(
            'https://openrouter.ai/api/v1/chat/completions',
            data=json.dumps(payload).encode('utf-8'),
            headers=headers,
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                choices = data.get('choices') or []
                if choices and choices[0].get('message', {}).get('content'):
                    return choices[0]['message']['content'].strip()
        except urllib.error.HTTPError as he:
            err_msg = f"HTTP {he.code}"
            try:
                err_msg += f": {he.read().decode('utf-8')[:120]}"
            except Exception:
                pass
            last_error = err_msg
            continue
        except Exception as e:
            last_error = str(e)
            continue

    if last_error:
        raise RuntimeError(last_error)
    return None


@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found', 'path': request.path}), 404
    return render_template('public_index.html'), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({
        'error': '405 Method Not Allowed',
        'request_path': request.path,
        'path_info': request.environ.get('PATH_INFO'),
        'query_string': request.environ.get('QUERY_STRING'),
        'method': request.method
    }), 405


@app.route('/')
@app.route('/api/index')
def index():
    return render_template('public_index.html')


@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(str(BASE_DIR / 'static'), filename)

@app.get('/research-console')
def research_console():
    # Internal research/reviewer UI. All sensitive API actions still require role-based access.
    return render_template('research_console.html')

@app.get('/api/status')
def api_status():
    if session.get('role') == 'tax_expert':
        return jsonify({'ready':True,'role':'tax_expert','workspace':'legal-validation-only'}),200
    with get_db() as db:
        qn=db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
        an=db.execute('SELECT COUNT(*) FROM answers').fetchone()[0]
        rn=db.execute('SELECT COUNT(*) FROM retrieval_items').fetchone()[0]
        backend=bool(os.environ.get('RAG_API_URL','').strip() or os.environ.get('OPENROUTER_API_KEY','').strip())
        return jsonify({'ready':True,'questions':qn,'answers':an,'retrieval_records':rn,'live_backend_configured':backend,'run_id':RUN_ID,'legal_accuracy_status':'Pending expert adjudication','prototype_mode':not backend})

@app.route('/api/query/stream', methods=['POST', 'GET'])
def api_query_stream():
    if request.method == 'GET':
        return jsonify({'status': 'stream_endpoint_ready', 'method': 'POST'}), 200
    payload=request.get_json(silent=True) or {}
    question=(payload.get('question') or '').strip()
    legal_date=(payload.get('legal_date') or '').strip()
    tax_period=(payload.get('tax_period') or '').strip()[:120]
    mode=payload.get('mode') or 'compare'
    if len(question)<8:
        return jsonify({'error':'Please enter a more specific question.'}),400
    if len(question)>3000:
        return jsonify({'error':'Question is too long (maximum 3,000 characters).'}),400
    if legal_date:
        try:
            datetime.strptime(legal_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error':'Please provide the legal date in YYYY-MM-DD format.'}),400
    selected=['A_CONVENTIONAL_RAG','B_BASIC_TIME_FILTERED_RAG','C_AMENDMENT_AWARE_TEMPORAL_RAG'] if mode=='compare' else [mode]
    def generate():
        yield make_sse({'type':'progress','step':1,'total':6,'percent':8,'label':'Checking your question','detail':'Validating the question and selected legal date.'})
        time.sleep(0.15)
        try:
            with get_db() as db:
                yield make_sse({'type':'progress','step':2,'total':6,'percent':22,'label':'Checking the research corpus','detail':'Looking for an exact match in the saved controlled experiment; otherwise searching the local legal records.'})
                archived=answer_archive_for_question(db,question)
                time.sleep(0.12)
                if archived:
                    yield make_sse({'type':'progress','step':3,'total':6,'percent':45,'label':'Loading saved experiment evidence','detail':f"Found saved question {archived['question_id']}. The archived answers will not be regenerated."})
                    time.sleep(0.12)
                    systems=[x for x in archived['systems'] if x['system_id'] in selected]
                    yield make_sse({'type':'progress','step':4,'total':6,'percent':68,'label':'Checking retrieved source records','detail':'Loading Top-5 records and source metadata saved with the controlled run.'})
                    yield make_sse({'type':'progress','step':5,'total':6,'percent':88,'label':'Preparing comparison','detail':'Preparing the selected system answers, evidence, and run provenance.'})
                    result={'mode':'archived_controlled_run','question_id':archived['question_id'],'question_en':archived['question_en'],'target_tax_period':archived['target_tax_period'],'legal_date_entered':legal_date,'systems':systems,'notice':'This is a saved output from the controlled run. The legal date entered now does not rerun the model or change the archived answer. Review the original experiment scope and evidence before interpreting it.'}
                else:
                    yield make_sse({'type':'progress','step':3,'total':6,'percent':40,'label':'Searching legal records','detail':'Running a local BM25-style lexical search over the bundled runtime corpus.'})
                    results=[]
                    for sid in selected:
                        evidence=search_local_corpus(db,question,legal_date,sid)
                        results.append({'system_id':sid,'system_name':SYSTEM_NAMES[sid],'evidence':evidence,'answer':None,'archived':False})
                    yield make_sse({'type':'progress','step':4,'total':6,'percent':65,'label':'Checking temporal metadata','detail':'Applying only explicit interval exclusions. Records without verified intervals are flagged as uncertain.'})
                    live=None
                    live_error=''
                    if os.environ.get('RAG_API_URL','').strip():
                        try:
                            yield make_sse({'type':'progress','step':5,'total':6,'percent':78,'label':'Connecting to the configured RAG backend','detail':'Sending the question and legal date to the configured research adapter.'})
                            live=try_live_backend(question,legal_date,selected,tax_period)
                        except Exception as ex:
                            live_error=str(ex)
                            live=None
                    elif os.environ.get('OPENROUTER_API_KEY','').strip():
                        try:
                            m_name=os.environ.get('OPENROUTER_MODEL','qwen/qwen-2.5-7b-instruct')
                            yield make_sse({'type':'progress','step':5,'total':6,'percent':78,'label':'Generating live answers with LLM','detail':f'Querying {m_name} for {len(results)} system(s) in parallel...'})
                            live_err_details = []
                            def _fetch_sys_answer(item):
                                sid = item['system_id']
                                ev = item['evidence']
                                ans = None
                                try:
                                    ans = generate_openrouter_answer(question, legal_date, sid, ev, tax_period)
                                except Exception as ex:
                                    live_err_details.append(f"{sid}: {str(ex)}")
                                return {'system_id': sid, 'system_name': item['system_name'], 'evidence': ev, 'answer': ans, 'archived': False}

                            with ThreadPoolExecutor(max_workers=min(len(results), 3)) as pool:
                                live_systems = list(pool.map(_fetch_sys_answer, results))

                            has_any_answer = any(bool(s.get('answer')) for s in live_systems)
                            if has_any_answer:
                                live = {'systems': live_systems}
                            else:
                                live = None
                                if live_err_details:
                                    live_error = "; ".join(live_err_details)
                        except Exception as ex:
                            live_error = str(ex)
                            live = None
                    if live:
                        notice_text = f"Live response generated via OpenRouter ({os.environ.get('OPENROUTER_MODEL','qwen/qwen-2.5-7b-instruct')}). Verify citations against official sources; this research prototype is not a substitute for professional tax advice." if os.environ.get('OPENROUTER_API_KEY','').strip() and not os.environ.get('RAG_API_URL','').strip() else 'Live backend response. Verify citations against official sources; this research prototype is not a substitute for professional tax advice.'
                        result = {'mode':'live_backend','question_id':None,'question_en':question,'target_tax_period':legal_date,'legal_date_entered':legal_date,'systems':live['systems'],'notice':notice_text}
                    else:
                        notice_text = 'No archived answer matched this question. The app has performed a local evidence search only; it has not generated a legal answer.'
                        if live_error:
                            notice_text += f' (Live LLM status: {live_error})'
                        else:
                            notice_text += ' The configured live RAG backend is not connected or did not respond. Treat these passages as research leads, not legal advice.'
                        result = {'mode':'local_evidence_preview','question_id':None,'question_en':question,'target_tax_period':legal_date,'legal_date_entered':legal_date,'systems':results,'notice':notice_text,'backend_error':live_error}
                    yield make_sse({'type':'progress','step':5,'total':6,'percent':88,'label':'Preparing evidence and limitations','detail':'Checking source metadata and preparing the results panel.'})
                db.execute('INSERT INTO query_logs(created_at,question,legal_date,mode,result_mode,success) VALUES(?,?,?,?,?,1)',(now_iso(),question,legal_date,mode,result['mode']))
                db.commit()
            yield make_sse({'type':'progress','step':6,'total':6,'percent':100,'label':'Ready','detail':'Results and their evaluation status are ready to inspect.'})
            yield make_sse({'type':'result','result':result})
        except Exception as e:
            yield make_sse({'type':'error','message':'The request could not be completed. '+str(e)})
    return Response(generate(),mimetype='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no','Connection':'close'})

@app.post('/api/login')
def api_login():
    p=request.get_json(silent=True) or {}
    user=(p.get('reviewer_id') or '').strip()
    password=p.get('access_code') or ''
    role=p.get('role') or 'reviewer'
    if not re.fullmatch(r'[A-Za-z0-9._@-]{2,60}',user):
        return jsonify({'error':'Enter an ID (2–60 letters, numbers, dots, underscores, @, or hyphens).'}),400
    if role not in ('reviewer','tax_expert','admin'):
        return jsonify({'error':'Choose a valid workspace role.'}),400
    if role=='admin':
        expected=os.environ.get('ADMIN_ACCESS_CODE','admin-demo-change-me')
    elif role=='tax_expert':
        expected=os.environ.get('TAX_EXPERT_ACCESS_CODE','tax-expert-demo-change-me')
    else:
        expected=os.environ.get('REVIEWER_ACCESS_CODE','review-demo-change-me')
    if not secrets.compare_digest(str(password),str(expected)):
        return jsonify({'error':'The access code is incorrect.'}),401
    session.clear(); session['reviewer_id']=user; session['role']=role; session['logged_in_at']=now_iso()
    return jsonify({'ok':True,'reviewer_id':user,'role':role})

@app.post('/api/logout')
def api_logout():
    session.clear(); return jsonify({'ok':True})

@app.get('/api/me')
def api_me():
    return jsonify({'logged_in':bool(session.get('reviewer_id')),'reviewer_id':session.get('reviewer_id'),'role':session.get('role')})

@app.get('/api/tax-expert/gold/next')
@tax_expert_required
def tax_expert_gold_next():
    """A minimal, blinded gold-answer form for an external tax professional."""
    user=session['reviewer_id']
    with get_db() as db:
        row=db.execute("""SELECT q.question_id,q.question_en,q.target_tax_period,q.target_as_of_date
            FROM questions q
            WHERE q.gold_status!='APPROVED'
              AND (SELECT COUNT(*) FROM gold_review_submissions s WHERE s.question_id=q.question_id)<2
              AND NOT EXISTS (SELECT 1 FROM gold_review_submissions mine WHERE mine.question_id=q.question_id AND mine.reviewer_id=?)
            ORDER BY q.question_id LIMIT 1""",(user,)).fetchone()
        done=db.execute('SELECT COUNT(DISTINCT question_id) FROM gold_review_submissions WHERE reviewer_id=?',(user,)).fetchone()[0]
        if not row:
            total=db.execute('SELECT COUNT(*) FROM questions').fetchone()[0]
            return jsonify({'task':None,'message':'No questions are available for this account. Your completed responses are saved. If all questions already have two independent reviews, administrator approval is the next step.','queue_done':done,'queue_total':total})
        task=dict(row)
        slots=db.execute('SELECT reviewer_slot FROM gold_review_submissions WHERE question_id=?',(task['question_id'],)).fetchall()
        used={int(x['reviewer_slot']) for x in slots}
        slot=next((n for n in (1,2) if n not in used),None)
        if slot is None:
            return jsonify({'task':None,'message':'This question already has two independent reviews.','queue_done':done,'queue_total':200})
        # Deliberately expose only the question and its target period/date. No generated answers,
        # system identities, ranking data, draft gold answers, or internal corpus metadata.
        return jsonify({'task':task,'assigned_slot':slot,'queue_done':done,'queue_total':200})

@app.post('/api/tax-expert/gold/submit')
@tax_expert_required
def tax_expert_gold_submit():
    p=request.get_json(silent=True) or {}
    qid=str(p.get('question_id','')).strip()
    try: slot=int(p.get('assigned_slot',0))
    except (TypeError,ValueError): slot=0
    if not qid or slot not in (1,2):
        return jsonify({'error':'Missing question or review assignment.'}),400
    allowed_status={'ANSWERABLE','INSUFFICIENT_EVIDENCE','OUT_OF_SCOPE','NEEDS_FURTHER_REVIEW'}
    allowed_confidence={'HIGH','MEDIUM','LOW'}
    allowed_role={'ACCOUNTANT','TAX_CONSULTANT','TAX_LAWYER','OTHER'}
    status=str(p.get('answerability_status','') or '').upper()
    confidence=str(p.get('confidence','') or '').upper()
    expert_role=str(p.get('professional_role','') or '').upper()
    answer=str(p.get('expert_answer_en','') or '').strip()[:12000]
    section=str(p.get('applicable_section','') or '').strip()[:1000]
    citation=str(p.get('official_citation','') or '').strip()[:2000]
    source_url=str(p.get('official_source_url','') or '').strip()[:2000]
    tax_period=str(p.get('tax_period','') or '').strip()[:300]
    as_of_date=str(p.get('as_of_date','') or '').strip()[:20]
    valid_from=str(p.get('valid_time_start','') or '').strip()[:20]
    valid_to=str(p.get('valid_time_end','') or '').strip()[:20]
    notes=str(p.get('expert_notes','') or '').strip()[:8000]
    experience=str(p.get('years_tax_experience','') or '').strip()[:4]
    if status not in allowed_status:return jsonify({'error':'Choose whether the question is answerable, out of scope, or needs further evidence.'}),400
    if confidence not in allowed_confidence:return jsonify({'error':'Select a confidence level.'}),400
    if expert_role not in allowed_role:return jsonify({'error':'Select your professional role.'}),400
    for name,value in [('as_of_date',as_of_date),('valid_time_start',valid_from),('valid_time_end',valid_to)]:
        if value:
            try: datetime.strptime(value,'%Y-%m-%d')
            except ValueError:return jsonify({'error':f'{name} must use YYYY-MM-DD.'}),400
    if valid_from and valid_to and valid_from>valid_to:
        return jsonify({'error':'The validity start date must not be later than the end date.'}),400
    if source_url and not re.match(r'^https?://',source_url,re.I):
        return jsonify({'error':'The source URL must begin with http:// or https://.'}),400
    if status=='ANSWERABLE' and not all([answer,section,citation]):
        return jsonify({'error':'For an answerable question, provide the answer, relevant statutory section, and official citation.'}),400
    if status!='ANSWERABLE' and not notes:
        return jsonify({'error':'Please explain why the question is out of scope or why the available evidence is insufficient.'}),400
    if experience:
        try:
            if not 0<=int(experience)<=60: raise ValueError()
        except ValueError:return jsonify({'error':'Years of tax experience must be between 0 and 60.'}),400
    user=session['reviewer_id']
    payload={
        'expert_answer_en':answer,'applicable_section':section,'official_citation':citation,'official_source_url':source_url,
        'tax_period':tax_period,'as_of_date':as_of_date,'valid_time_start':valid_from,'valid_time_end':valid_to,
        'answerability_status':status,'confidence':confidence,'professional_role':expert_role,
        'years_tax_experience':experience,'expert_notes':notes,
        # Compatibility fields allow the administrator to inspect this response in the existing approval flow.
        'approved_reference_answer_en':answer,'approved_gold_record_ids':'',
        'approved_statutory_citation':citation,'target_tax_period':tax_period,'target_as_of_date':as_of_date,
        'valid_time_start':valid_from,'valid_time_end':valid_to,
        'adjudication_status':{'ANSWERABLE':'REVIEWER_APPROVED','INSUFFICIENT_EVIDENCE':'INSUFFICIENT_EVIDENCE','OUT_OF_SCOPE':'OUT_OF_SCOPE','NEEDS_FURTHER_REVIEW':'NEEDS_DISCUSSION'}[status],
        'review_notes':notes
    }
    with get_db() as db:
        if not db.execute('SELECT 1 FROM questions WHERE question_id=?',(qid,)).fetchone():
            return jsonify({'error':'Question not found.'}),404
        other=db.execute('SELECT reviewer_id FROM gold_review_submissions WHERE question_id=? AND reviewer_slot=?',(qid,slot)).fetchone()
        if other and other['reviewer_id']!=user:
            return jsonify({'error':'This question has already been reviewed in this assignment slot.'}),409
        same=db.execute('SELECT reviewer_id FROM gold_review_submissions WHERE question_id=? AND reviewer_id=?',(qid,user)).fetchone()
        if same:
            return jsonify({'error':'This question has already been submitted by your account.'}),409
        db.execute("""INSERT OR REPLACE INTO gold_review_submissions(question_id,reviewer_slot,reviewer_id,payload_json,submitted_at)
            VALUES(?,?,?,?,?)""",(qid,slot,user,json.dumps(payload,ensure_ascii=False),now_iso()))
        db.commit()
        done=db.execute('SELECT COUNT(DISTINCT question_id) FROM gold_review_submissions WHERE reviewer_id=?',(user,)).fetchone()[0]
    return jsonify({'ok':True,'done':done,'total':200,'message':'Your legal assessment has been saved. Continue to the next question.'})

@app.get('/api/tax-expert/answer/next')
@tax_expert_required
def tax_expert_answer_next():
    """Minimal blinded review of one saved generated answer, without exposing system identity or retrieval internals."""
    user=session['reviewer_id']
    with get_db() as db:
        row=db.execute("""SELECT t.review_row_id,t.question_id,t.reviewer_slot,q.question_en,q.target_tax_period,q.target_as_of_date,a.generated_answer
            FROM answer_review_tasks t
            JOIN questions q ON q.question_id=t.question_id
            JOIN answers a ON a.question_id=t.question_id AND a.system_id=(SELECT value FROM settings WHERE key='private_code_'||t.review_system_code)
            WHERE t.status='PENDING'
              AND NOT EXISTS (SELECT 1 FROM answer_review_tasks other WHERE other.question_id=t.question_id AND other.review_system_code=t.review_system_code AND other.reviewer_slot!=t.reviewer_slot AND other.status='SUBMITTED' AND other.reviewer_id=?)
            ORDER BY t.question_id,t.review_system_code,t.reviewer_slot LIMIT 1""",(user,)).fetchone()
        done=db.execute("SELECT COUNT(*) FROM answer_review_tasks WHERE status='SUBMITTED' AND reviewer_id=? AND payload_json LIKE '%\"tax_expert_form\": true%'",(user,)).fetchone()[0]
        if not row:
            return jsonify({'task':None,'message':'No generated-answer reviews are available for this account. Your saved assessments remain available for download.','queue_done':done,'queue_total':600})
        # Do not expose the internal system ID, blind code, retrieved context, ranking or mappings.
        d=dict(row)
        return jsonify({'task':{'review_row_id':d['review_row_id'],'question_id':d['question_id'],'question_en':d['question_en'],'target_tax_period':d['target_tax_period'],'target_as_of_date':d['target_as_of_date'],'generated_answer':d['generated_answer']},'queue_done':done,'queue_total':600})

@app.post('/api/tax-expert/answer/submit')
@tax_expert_required
def tax_expert_answer_submit():
    p=request.get_json(silent=True) or {}
    task_id=str(p.get('review_row_id','')).strip()
    user=session['reviewer_id']
    payload={}
    required=['substantive_correctness','temporal_version_correctness','answer_completeness_conditions','citation_entailment','answer_or_abstention_appropriate']
    for field,rule in ANSWER_REVIEW_FIELDS.items():
        value=p.get(field,'')
        if rule=='text': value=str(value or '')[:5000]
        elif rule=='number_0_100':
            if value not in ('',None):
                try: value=max(0,min(100,float(value)))
                except Exception: return jsonify({'error':f'{field} must be between 0 and 100.'}),400
        elif rule=='number_0_1':
            if value not in ('',None):
                try: value=max(0,min(1,float(value)))
                except Exception: return jsonify({'error':f'{field} must be between 0 and 1.'}),400
        elif value and value not in rule:
            return jsonify({'error':f'Invalid value for {field}.'}),400
        payload[field]=value
    if not task_id:return jsonify({'error':'Missing answer-review task.'}),400
    if not all(payload.get(k) for k in required):return jsonify({'error':'Please complete all five required ratings.'}),400
    payload['tax_expert_form']=True
    with get_db() as db:
        task=db.execute('SELECT * FROM answer_review_tasks WHERE review_row_id=?',(task_id,)).fetchone()
        if not task:return jsonify({'error':'Review task not found.'}),404
        if task['status']=='SUBMITTED' and task['reviewer_id']!=user:return jsonify({'error':'This review task has already been submitted.'}),409
        other=db.execute('SELECT reviewer_id FROM answer_review_tasks WHERE question_id=? AND review_system_code=? AND reviewer_slot!=? AND status="SUBMITTED"',(task['question_id'],task['review_system_code'],task['reviewer_slot'])).fetchone()
        if other and other['reviewer_id']==user:return jsonify({'error':'A different reviewer must assess the other blind copy of this answer.'}),409
        # Re-submission of an already-submitted task is allowed only for the same reviewer.
        db.execute('UPDATE answer_review_tasks SET reviewer_id=?,status="SUBMITTED",payload_json=?,reviewed_at=? WHERE review_row_id=?',(user,json.dumps(payload,ensure_ascii=False),now_iso(),task_id))
        db.commit()
        done=db.execute("SELECT COUNT(*) FROM answer_review_tasks WHERE status='SUBMITTED' AND reviewer_id=? AND payload_json LIKE '%\"tax_expert_form\": true%'",(user,)).fetchone()[0]
    return jsonify({'ok':True,'done':done,'total':600,'message':'Your answer assessment has been saved.'})

@app.get('/api/tax-expert/answer-assessments.csv')
@tax_expert_required
def tax_expert_export_own_answer_assessments():
    user=session['reviewer_id']
    with get_db() as db:
        exports=build_exports(db)
        text=exports['TAX_EXPERT_ANSWER_ASSESSMENTS.csv'].decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(text)))
        own=[r for r in rows if r.get('reviewer_id')==user]
        # Public CSV must not reveal blind system codes or internal task IDs.
        headers=['question_id','question_en','target_tax_period','target_as_of_date','reviewer_id','review_slot','submitted_at','generated_answer','substantive_correctness','temporal_version_correctness','answer_completeness_conditions','citation_entailment','answer_or_abstention_appropriate','unsupported_claims_present','review_notes']
        own=[{k:r.get(k,'') for k in headers} for r in own]
    return send_file(io.BytesIO(csv_bytes(headers,own)),mimetype='text/csv; charset=utf-8',as_attachment=True,download_name=f'tax_expert_answer_assessments_{user}.csv')

@app.get('/api/tax-expert/export.csv')
@tax_expert_required
def tax_expert_export_own_responses():
    user=session['reviewer_id']
    with get_db() as db:
        exports=build_exports(db)
        text=exports['TAX_EXPERT_REVIEW_RESPONSES.csv'].decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(text)))
        own=[r for r in rows if r.get('reviewer_id')==user]
        headers=list(rows[0].keys()) if rows else ['question_id','question_en','reviewer_id','submitted_at','expert_answer_en','applicable_section','official_citation','official_source_url','tax_period','as_of_date','valid_time_start','valid_time_end','answerability_status','confidence','professional_role','years_tax_experience','expert_notes']
    return send_file(io.BytesIO(csv_bytes(headers,own)),mimetype='text/csv; charset=utf-8',as_attachment=True,download_name=f'tax_expert_responses_{user}.csv')

@app.get('/api/review/answer/next')
@reviewer_required
def answer_review_next():
    slot=int(request.args.get('slot','1'))
    if slot not in (1,2): return jsonify({'error':'Slot must be 1 or 2.'}),400
    user=session['reviewer_id']
    with get_db() as db:
        row=db.execute('''SELECT t.*, q.target_tax_period, q.target_as_of_date, q.question_en, a.system_name, a.generated_answer, a.actual_context_text, a.top5_parent_ids, a.context_sha256, a.prompt_sha256, a.latency_seconds
            FROM answer_review_tasks t JOIN questions q ON q.question_id=t.question_id
            JOIN answers a ON a.question_id=t.question_id AND a.system_id=(SELECT value FROM settings WHERE key='private_code_'||t.review_system_code)
            WHERE t.reviewer_slot=? AND t.status='PENDING' ORDER BY t.question_id, t.review_system_code LIMIT 1''',(slot,)).fetchone()
        if row:
            # Prevent the same human rating both blind copies of the same answer.
            opposite=db.execute('''SELECT reviewer_id FROM answer_review_tasks WHERE question_id=? AND review_system_code=? AND reviewer_slot!=? AND status='SUBMITTED' ''',(row['question_id'],row['review_system_code'],slot)).fetchone()
            if opposite and opposite['reviewer_id']==user:
                row=db.execute('''SELECT t.*, q.target_tax_period, q.target_as_of_date, q.question_en, a.system_name, a.generated_answer, a.actual_context_text, a.top5_parent_ids, a.context_sha256, a.prompt_sha256, a.latency_seconds
                    FROM answer_review_tasks t JOIN questions q ON q.question_id=t.question_id
                    JOIN answers a ON a.question_id=t.question_id AND a.system_id=(SELECT value FROM settings WHERE key='private_code_'||t.review_system_code)
                    WHERE t.reviewer_slot=? AND t.status='PENDING' AND NOT EXISTS (SELECT 1 FROM answer_review_tasks other WHERE other.question_id=t.question_id AND other.review_system_code=t.review_system_code AND other.reviewer_slot!=t.reviewer_slot AND other.status='SUBMITTED' AND other.reviewer_id=?) ORDER BY t.question_id,t.review_system_code LIMIT 1''',(slot,user)).fetchone()
        if not row: return jsonify({'task':None,'message':'No pending answer-review tasks for this reviewer slot.'})
        d=dict(row); d['payload']=json.loads(d.pop('payload_json') or '{}')
        # Never include hidden system ID/name or codebook mapping in blind review API.
        d.pop('system_name',None); d['review_system_code']=d['review_system_code']
        d['assigned_slot']=slot; d['fields']=ANSWER_REVIEW_FIELDS
        progress=db.execute("SELECT SUM(CASE WHEN status='SUBMITTED' THEN 1 ELSE 0 END) done, COUNT(*) total FROM answer_review_tasks WHERE reviewer_slot=?",(slot,)).fetchone()
        d['queue_done']=progress['done'] or 0; d['queue_total']=progress['total']
        return jsonify({'task':d})

@app.post('/api/review/answer/submit')
@reviewer_required
def answer_review_submit():
    p=request.get_json(silent=True) or {}; task_id=p.get('review_row_id'); slot=int(p.get('reviewer_slot',0)); user=session['reviewer_id']
    if slot not in (1,2) or not task_id: return jsonify({'error':'Missing review task or slot.'}),400
    payload={}
    for field, rule in ANSWER_REVIEW_FIELDS.items():
        value=p.get(field,'')
        if rule=='text': value=str(value)[:5000]
        elif rule=='number_0_100':
            if value not in ('',None):
                try: value=max(0,min(100,float(value)))
                except: return jsonify({'error':f'{field} must be between 0 and 100.'}),400
        elif rule=='number_0_1':
            if value not in ('',None):
                try: value=max(0,min(1,float(value)))
                except: return jsonify({'error':f'{field} must be between 0 and 1.'}),400
        elif value and value not in rule: return jsonify({'error':f'Invalid value for {field}.'}),400
        payload[field]=value
    if not payload.get('substantive_correctness') or not payload.get('temporal_version_correctness') or not payload.get('answer_completeness_conditions'):
        return jsonify({'error':'Please score substantive correctness, temporal/version correctness, and completeness.'}),400
    with get_db() as db:
        task=db.execute('SELECT * FROM answer_review_tasks WHERE review_row_id=?',(task_id,)).fetchone()
        if not task or task['reviewer_slot']!=slot: return jsonify({'error':'This task is not assigned to the selected reviewer slot.'}),403
        other=db.execute('SELECT reviewer_id FROM answer_review_tasks WHERE question_id=? AND review_system_code=? AND reviewer_slot!=? AND status="SUBMITTED"',(task['question_id'],task['review_system_code'],slot)).fetchone()
        if other and other['reviewer_id']==user: return jsonify({'error':'For blinded independent review, another reviewer must complete the other slot for this answer.'}),409
        if task['status']=='SUBMITTED' and task['reviewer_id']!=user: return jsonify({'error':'This task has already been submitted by another reviewer.'}),409
        db.execute('UPDATE answer_review_tasks SET reviewer_id=?,status="SUBMITTED",payload_json=?,reviewed_at=? WHERE review_row_id=?',(user,json.dumps(payload,ensure_ascii=False),now_iso(),task_id)); db.commit()
        progress=db.execute('SELECT SUM(CASE WHEN status="SUBMITTED" THEN 1 ELSE 0 END) done, COUNT(*) total FROM answer_review_tasks WHERE reviewer_slot=?',(slot,)).fetchone()
    return jsonify({'ok':True,'done':progress['done'],'total':progress['total']})

@app.get('/api/review/retrieval/next')
@reviewer_required
def retrieval_review_next():
    slot=int(request.args.get('slot','1')); user=session['reviewer_id']
    if slot not in (1,2): return jsonify({'error':'Slot must be 1 or 2.'}),400
    with get_db() as db:
        row=db.execute('''SELECT r.*, q.target_tax_period, q.target_as_of_date, c.chunk_text, c.retrieval_context, c.retrieval_text, c.record_status, c.research_freeze_status, c.retrieval_use_policy
        FROM retrieval_items r JOIN questions q ON q.question_id=r.question_id
        LEFT JOIN chunks c ON c.record_id=r.parent_record_id AND c.chunk_id=(SELECT MIN(c2.chunk_id) FROM chunks c2 WHERE c2.record_id=r.parent_record_id)
        WHERE NOT EXISTS (SELECT 1 FROM retrieval_review_submissions s WHERE s.item_id=r.item_id AND s.reviewer_slot=?)
        AND NOT EXISTS (SELECT 1 FROM retrieval_review_submissions other WHERE other.item_id=r.item_id AND other.reviewer_id=?)
        ORDER BY r.question_id, r.system_id, r.parent_rank LIMIT 1''',(slot,user)).fetchone()
        if not row: return jsonify({'task':None,'message':'No pending retrieval-review items for this reviewer and slot.'})
        d=dict(row); d.pop('system_id',None)  # Keep the independent review blinded.
        d['review_system_code']=d['review_system_code']; d['fields']=RETRIEVAL_FIELDS; d['assigned_slot']=slot
        d['queue_done']=db.execute('SELECT COUNT(*) FROM retrieval_review_submissions WHERE reviewer_slot=?',(slot,)).fetchone()[0]
        d['queue_total']=retrieval_total = db.execute('SELECT COUNT(*) FROM retrieval_items').fetchone()[0]
        return jsonify({'task':d})

@app.post('/api/review/retrieval/submit')
@reviewer_required
def retrieval_review_submit():
    p=request.get_json(silent=True) or {}; item_id=int(p.get('item_id',0)); slot=int(p.get('reviewer_slot',0)); user=session['reviewer_id']
    if slot not in (1,2) or not item_id: return jsonify({'error':'Missing retrieval item or reviewer slot.'}),400
    payload={}
    for field,rule in RETRIEVAL_FIELDS.items():
        val=p.get(field,'')
        if rule=='text': val=str(val)[:5000]
        elif val and val not in rule: return jsonify({'error':f'Invalid value for {field}.'}),400
        payload[field]=val
    if not payload['record_is_correct_statutory_source'] or not payload['record_version_valid_for_target_period'] or not payload['future_amendment_leakage']:
        return jsonify({'error':'Please complete source correctness, version validity, and future-amendment leakage.'}),400
    with get_db() as db:
        it=db.execute('SELECT * FROM retrieval_items WHERE item_id=?',(item_id,)).fetchone()
        if not it:return jsonify({'error':'Retrieval item not found.'}),404
        other=db.execute('SELECT reviewer_id FROM retrieval_review_submissions WHERE item_id=? AND reviewer_slot!=?',(item_id,slot)).fetchone()
        if other and other['reviewer_id']==user:return jsonify({'error':'Use a different reviewer for the independent review slot.'}),409
        existing=db.execute('SELECT reviewer_id FROM retrieval_review_submissions WHERE item_id=? AND reviewer_slot=?',(item_id,slot)).fetchone()
        if existing and existing['reviewer_id']!=user:return jsonify({'error':'This review slot has already been completed.'}),409
        db.execute('INSERT OR REPLACE INTO retrieval_review_submissions(item_id,reviewer_slot,reviewer_id,payload_json,submitted_at) VALUES(?,?,?,?,?)',(item_id,slot,user,json.dumps(payload,ensure_ascii=False),now_iso()))
        db.commit()
        prog=db.execute('SELECT COUNT(*) FROM retrieval_items r WHERE (SELECT COUNT(*) FROM retrieval_review_submissions s WHERE s.item_id=r.item_id)>=2').fetchone()[0]
    return jsonify({'ok':True,'double_reviewed_items':prog,'total_items':3000})

@app.get('/api/review/gold/next')
@reviewer_required
def gold_review_next():
    slot=int(request.args.get('slot','1')); user=session['reviewer_id']
    if slot not in (1,2): return jsonify({'error':'Slot must be 1 or 2.'}),400
    with get_db() as db:
        row=db.execute('''SELECT q.* FROM questions q WHERE NOT EXISTS (SELECT 1 FROM gold_review_submissions s WHERE s.question_id=q.question_id AND s.reviewer_slot=?)
        AND NOT EXISTS (SELECT 1 FROM gold_review_submissions other WHERE other.question_id=q.question_id AND other.reviewer_id=?) ORDER BY q.question_id LIMIT 1''',(slot,user)).fetchone()
        if not row:return jsonify({'task':None,'message':'No pending gold-label review tasks for this reviewer slot.'})
        reviews=db.execute('SELECT reviewer_slot,reviewer_id,submitted_at FROM gold_review_submissions WHERE question_id=?',(row['question_id'],)).fetchall()
        done=db.execute('SELECT COUNT(*) FROM gold_review_submissions WHERE reviewer_slot=?',(slot,)).fetchone()[0]
        return jsonify({'task':dict(row),'assigned_slot':slot,'reviews':[dict(r) for r in reviews],'queue_done':done,'queue_total':200})

@app.post('/api/review/gold/submit')
@reviewer_required
def gold_review_submit():
    p=request.get_json(silent=True) or {}; qid=p.get('question_id',''); slot=int(p.get('reviewer_slot',0)); user=session['reviewer_id']
    if slot not in (1,2) or not qid:return jsonify({'error':'Missing question or reviewer slot.'}),400
    payload={k:str(p.get(k,'') or '')[:10000] for k in ['approved_reference_answer_en','approved_gold_record_ids','approved_statutory_citation','target_tax_period','target_as_of_date','valid_time_start','valid_time_end','adjudication_status','review_notes']}
    if not payload['approved_reference_answer_en'] or not payload['approved_gold_record_ids'] or not payload['approved_statutory_citation']:
        return jsonify({'error':'Provide an expected answer, supporting record IDs, and a statutory citation.'}),400
    if payload['adjudication_status'] not in ('REVIEWER_APPROVED','NEEDS_DISCUSSION','OUT_OF_SCOPE','INSUFFICIENT_EVIDENCE'):
        return jsonify({'error':'Choose a valid review status.'}),400
    with get_db() as db:
        if not db.execute('SELECT 1 FROM questions WHERE question_id=?',(qid,)).fetchone():return jsonify({'error':'Question not found.'}),404
        other=db.execute('SELECT reviewer_id FROM gold_review_submissions WHERE question_id=? AND reviewer_slot!=?',(qid,slot)).fetchone()
        if other and other['reviewer_id']==user:return jsonify({'error':'Use a different reviewer for the independent gold review.'}),409
        existing=db.execute('SELECT reviewer_id FROM gold_review_submissions WHERE question_id=? AND reviewer_slot=?',(qid,slot)).fetchone()
        if existing and existing['reviewer_id']!=user:return jsonify({'error':'This gold-label review slot is already assigned.'}),409
        db.execute('INSERT OR REPLACE INTO gold_review_submissions(question_id,reviewer_slot,reviewer_id,payload_json,submitted_at) VALUES(?,?,?,?,?)',(qid,slot,user,json.dumps(payload,ensure_ascii=False),now_iso()))
        db.commit()
        done=db.execute('SELECT COUNT(*) FROM questions q WHERE (SELECT COUNT(*) FROM gold_review_submissions g WHERE g.question_id=q.question_id)>=2').fetchone()[0]
    return jsonify({'ok':True,'double_reviewed_questions':done,'total_questions':200})

@app.get('/api/admin/gold/next')
@admin_required
def admin_gold_next():
    with get_db() as db:
        row=db.execute('''SELECT q.* FROM questions q WHERE q.gold_status!='APPROVED' AND (SELECT COUNT(*) FROM gold_review_submissions s WHERE s.question_id=q.question_id)>=2 ORDER BY q.question_id LIMIT 1''').fetchone()
        if not row:return jsonify({'task':None,'message':'No questions have two completed independent reviews awaiting approval.'})
        reviews=db.execute('SELECT * FROM gold_review_submissions WHERE question_id=? ORDER BY reviewer_slot',(row['question_id'],)).fetchall()
        return jsonify({'task':dict(row),'reviews':[{'reviewer_slot':r['reviewer_slot'],'reviewer_id':r['reviewer_id'],'submitted_at':r['submitted_at'],'payload':json.loads(r['payload_json'])} for r in reviews]})

@app.post('/api/admin/gold/approve')
@admin_required
def admin_gold_approve():
    p=request.get_json(silent=True) or {}; qid=p.get('question_id','')
    fields={k:str(p.get(k,'') or '')[:10000] for k in ['approved_reference_answer_en','approved_gold_record_ids','approved_statutory_citation','target_tax_period','target_as_of_date','valid_time_start','valid_time_end','gold_review_notes']}
    if not all(fields[k] for k in ['approved_reference_answer_en','approved_gold_record_ids','approved_statutory_citation']):return jsonify({'error':'Answer, gold record IDs, and statutory citation are required for approval.'}),400
    with get_db() as db:
        n=db.execute('SELECT COUNT(*) FROM gold_review_submissions WHERE question_id=?',(qid,)).fetchone()[0]
        if n<2:return jsonify({'error':'Two independent expert reviews are required before approval.'}),409
        db.execute('''UPDATE questions SET approved_reference_answer_en=?,approved_gold_record_ids=?,approved_statutory_citation=?,target_tax_period=?,target_as_of_date=?,valid_time_start=?,valid_time_end=?,gold_status='APPROVED',gold_review_notes=?,approved_by=?,approved_at=? WHERE question_id=?''',(fields['approved_reference_answer_en'],fields['approved_gold_record_ids'],fields['approved_statutory_citation'],fields['target_tax_period'],fields['target_as_of_date'],fields['valid_time_start'],fields['valid_time_end'],fields['gold_review_notes'],session['reviewer_id'],now_iso(),qid))
        db.commit()
    return jsonify({'ok':True})

@app.get('/api/review/prompt/next')
@reviewer_required
def prompt_review_next():
    user=session['reviewer_id']
    with get_db() as db:
        row=db.execute('SELECT * FROM prompt_review_items WHERE review_status="PENDING" ORDER BY question_id,strategy_key LIMIT 1',()).fetchone()
        if not row:return jsonify({'task':None,'message':'No prompt-review items remain for this reviewer.'})
        d=dict(row); d['payload']=json.loads(d.pop('payload_json') or '{}'); d['fields']=PROMPT_FIELDS
        d['queue_done']=db.execute("SELECT COUNT(*) FROM prompt_review_items WHERE review_status='SUBMITTED'").fetchone()[0]
        d['queue_total']=db.execute('SELECT COUNT(*) FROM prompt_review_items').fetchone()[0]
        # expected answer is explicitly labelled draft and shown for review context only
        return jsonify({'task':d})

@app.post('/api/review/prompt/submit')
@reviewer_required
def prompt_review_submit():
    p=request.get_json(silent=True) or {}; task_id=int(p.get('task_id',0)); user=session['reviewer_id']; payload={}
    for f,rule in PROMPT_FIELDS.items():
        v=p.get(f,'')
        if rule=='text':v=str(v)[:5000]
        elif v and v not in rule:return jsonify({'error':f'Invalid rating for {f}.'}),400
        payload[f]=v
    if not all(payload[x] for x in ['human_factual_correctness_1to5','human_faithfulness_1to5','human_completeness_1to5','human_temporal_appropriateness_1to5','human_citation_accuracy_1to5']):return jsonify({'error':'Please complete all five ratings.'}),400
    with get_db() as db:
        row=db.execute('SELECT * FROM prompt_review_items WHERE task_id=?',(task_id,)).fetchone()
        if not row:return jsonify({'error':'Prompt review task not found.'}),404
        if row['review_status']=='SUBMITTED' and row['reviewer_id']!=user:return jsonify({'error':'This task has already been reviewed by another user.'}),409
        db.execute('UPDATE prompt_review_items SET payload_json=?,review_status="SUBMITTED",reviewer_id=?,reviewed_at=? WHERE task_id=?',(json.dumps(payload,ensure_ascii=False),user,now_iso(),task_id));db.commit()
        prog=db.execute('SELECT COUNT(*) done,(SELECT COUNT(*) FROM prompt_review_items) total FROM prompt_review_items WHERE review_status="SUBMITTED"').fetchone()
    return jsonify({'ok':True,'done':prog['done'],'total':prog['total']})

@app.get('/api/admin/answer-adjudication/next')
@admin_required
def admin_answer_adjudication_next():
    with get_db() as db:
        row=db.execute('''SELECT a.*, q.question_en, q.target_tax_period, an.generated_answer, an.actual_context_text, an.top5_parent_ids, an.context_sha256, an.prompt_sha256
        FROM answer_adjudications a JOIN questions q ON q.question_id=a.question_id
        JOIN answers an ON an.question_id=a.question_id AND an.system_id=a.system_id
        WHERE a.status!='ADJUDICATED'
        AND (SELECT COUNT(*) FROM answer_review_tasks t WHERE t.question_id=a.question_id AND t.review_system_code=a.review_system_code AND t.status='SUBMITTED')>=2
        ORDER BY a.question_id,a.review_system_code LIMIT 1''').fetchone()
        if not row:return jsonify({'task':None,'message':'No answers with two completed independent reviews are awaiting adjudication.'})
        reviews=db.execute('SELECT * FROM answer_review_tasks WHERE question_id=? AND review_system_code=? ORDER BY reviewer_slot',(row['question_id'],row['review_system_code'])).fetchall()
        d=dict(row); d['reviews']=[{'reviewer_slot':r['reviewer_slot'],'reviewer_id':r['reviewer_id'],'payload':json.loads(r['payload_json'] or '{}'),'reviewed_at':r['reviewed_at']} for r in reviews]
        d['fields']={'final_substantive_correctness':['CORRECT','PARTIALLY_CORRECT','INCORRECT','UNANSWERABLE','OUT_OF_SCOPE'],'final_temporal_version_correctness':['CORRECT','INCORRECT','UNCLEAR','NOT_APPLICABLE'],'final_completeness_conditions':['COMPLETE','PARTIAL','INCOMPLETE','NOT_APPLICABLE'],'final_citation_entailment':['SUPPORTED','PARTIALLY_SUPPORTED','UNSUPPORTED','NO_CITATION','NOT_APPLICABLE'],'final_actual_context_support':['SUPPORTED','PARTIAL','UNSUPPORTED','UNCLEAR'],'final_unsupported_claims_absent':['YES','NO','UNCLEAR'],'final_answer_or_abstention_appropriate':['APPROPRIATE','SHOULD_ABSTAIN','SHOULD_ANSWER','UNCLEAR'],'final_source_reference_checked':['YES','NO'],'final_valid_time_checked':['YES','NO']}
        return jsonify({'task':d})

@app.post('/api/admin/answer-adjudication/submit')
@admin_required
def admin_answer_adjudication_submit():
    p=request.get_json(silent=True) or {}; qid=p.get('question_id',''); code=p.get('review_system_code',''); fields=['final_substantive_correctness','final_temporal_version_correctness','final_completeness_conditions','final_section_subsection_correctness','final_citation_target_correctness','final_citation_entailment','final_actual_context_support','final_unsupported_claims_absent','final_answer_or_abstention_appropriate','final_answer_relevance','final_nugget_coverage_pct','final_citation_present','final_citation_precision','final_citation_recall','final_numeric_values_correct','final_contradiction_free','final_source_reference_checked','final_valid_time_checked','adjudication_notes']
    payload={k:str(p.get(k,'') or '')[:5000] for k in fields}
    if not payload['final_substantive_correctness'] or not payload['final_temporal_version_correctness'] or not payload['final_completeness_conditions']:return jsonify({'error':'Final correctness, temporal version, and completeness labels are required.'}),400
    with get_db() as db:
        n=db.execute('SELECT COUNT(*) FROM answer_review_tasks WHERE question_id=? AND review_system_code=? AND status="SUBMITTED"',(qid,code)).fetchone()[0]
        if n<2:return jsonify({'error':'Two independent reviews are required.'}),409
        db.execute('UPDATE answer_adjudications SET status="ADJUDICATED",adjudicator_id=?,adjudication_date=?,payload_json=? WHERE question_id=? AND review_system_code=?',(session['reviewer_id'],now_iso(),json.dumps(payload,ensure_ascii=False),qid,code));db.commit()
    return jsonify({'ok':True})

@app.get('/api/admin/retrieval-adjudication/next')
@admin_required
def admin_retrieval_adjudication_next():
    with get_db() as db:
        row=db.execute('''SELECT r.*,q.target_tax_period,c.chunk_text,c.retrieval_context,c.record_status,c.retrieval_use_policy
        FROM retrieval_items r JOIN questions q ON q.question_id=r.question_id LEFT JOIN chunks c ON c.record_id=r.parent_record_id AND c.chunk_id=(SELECT MIN(c2.chunk_id) FROM chunks c2 WHERE c2.record_id=r.parent_record_id)
        WHERE (SELECT COUNT(*) FROM retrieval_review_submissions s WHERE s.item_id=r.item_id)>=2
        AND NOT EXISTS (SELECT 1 FROM retrieval_adjudications a WHERE a.item_id=r.item_id AND a.final_label!='')
        ORDER BY r.question_id,r.system_id,r.parent_rank LIMIT 1''').fetchone()
        if not row:return jsonify({'task':None,'message':'No retrieval items with two reviews are waiting for adjudication.'})
        rv=db.execute('SELECT * FROM retrieval_review_submissions WHERE item_id=? ORDER BY reviewer_slot',(row['item_id'],)).fetchall()
        d=dict(row);d['reviews']=[{'reviewer_slot':x['reviewer_slot'],'reviewer_id':x['reviewer_id'],'payload':json.loads(x['payload_json'])} for x in rv]
        d['labels']=['VALID_RETRIEVAL','WRONG_SOURCE','WRONG_VERSION','FUTURE_AMENDMENT_LEAKAGE','UNCERTAIN','NOT_APPLICABLE']
        return jsonify({'task':d})

@app.post('/api/admin/retrieval-adjudication/submit')
@admin_required
def admin_retrieval_adjudication_submit():
    p=request.get_json(silent=True) or {}; item_id=int(p.get('item_id',0)); label=p.get('final_label','');note=str(p.get('review_notes','') or '')[:5000]
    allowed=['VALID_RETRIEVAL','WRONG_SOURCE','WRONG_VERSION','FUTURE_AMENDMENT_LEAKAGE','UNCERTAIN','NOT_APPLICABLE']
    if label not in allowed:return jsonify({'error':'Choose an adjudication label.'}),400
    with get_db() as db:
        n=db.execute('SELECT COUNT(*) FROM retrieval_review_submissions WHERE item_id=?',(item_id,)).fetchone()[0]
        if n<2:return jsonify({'error':'Two independent reviews are required.'}),409
        db.execute('INSERT OR REPLACE INTO retrieval_adjudications(item_id,final_adjudicator_id,final_label,payload_json,adjudicated_at) VALUES(?,?,?,?,?)',(item_id,session['reviewer_id'],label,json.dumps({'review_notes':note}),now_iso()));db.commit()
    return jsonify({'ok':True})

# Export helpers

def csv_bytes(headers, rows):
    s=io.StringIO(newline='');w=csv.DictWriter(s,fieldnames=headers,extrasaction='ignore');w.writeheader()
    for r in rows:w.writerow(r)
    return s.getvalue().encode('utf-8-sig')

def all_rows(db, sql, args=()): return [dict(r) for r in db.execute(sql,args).fetchall()]

def build_exports(db):
    exports={}
    # Gold approved only; keep separate review queue for pending labels.
    q=all_rows(db,'SELECT * FROM questions ORDER BY question_id')
    gold_headers=['question_id','question_en','approved_gold_record_ids','approved_reference_answer_en','approved_statutory_citation','target_tax_period','target_as_of_date','valid_time_start','valid_time_end','gold_status','approved_by','approved_at','gold_review_notes']
    exports['GOLD_LABELS_APPROVED_200Q.csv']=csv_bytes(gold_headers,[r for r in q if r['gold_status']=='APPROVED'])
    exports['GOLD_LABELS_REVIEW_QUEUE_200Q.csv']=csv_bytes(gold_headers,q)
    # Answer reviews 1200 rows with fields + answer/code metadata; avoid exposing mapping.
    headers=['review_row_id','question_id','review_system_code','reviewer_slot','question_en','target_tax_period','generated_answer','actual_context_text','top5_parent_record_ids','context_sha256','prompt_sha256','status','reviewer_id','reviewed_at']+list(ANSWER_REVIEW_FIELDS.keys())
    rows=[]
    for t in all_rows(db,'SELECT * FROM answer_review_tasks ORDER BY question_id,review_system_code,reviewer_slot'):
        code=t['review_system_code'];sid=db.execute('SELECT value FROM settings WHERE key=?',('private_code_'+code,)).fetchone()['value']
        a=db.execute('SELECT * FROM answers WHERE question_id=? AND system_id=?',(t['question_id'],sid)).fetchone(); qrow=db.execute('SELECT * FROM questions WHERE question_id=?',(t['question_id'],)).fetchone()
        payload=json.loads(t['payload_json'] or '{}')
        row={'review_row_id':t['review_row_id'],'question_id':t['question_id'],'review_system_code':code,'reviewer_slot':t['reviewer_slot'],'question_en':qrow['question_en'],'target_tax_period':qrow['target_tax_period'],'generated_answer':a['generated_answer'] if a else '', 'actual_context_text':a['actual_context_text'] if a else '', 'top5_parent_record_ids':a['top5_parent_ids'] if a else '', 'context_sha256':a['context_sha256'] if a else '', 'prompt_sha256':a['prompt_sha256'] if a else '', 'status':t['status'],'reviewer_id':t['reviewer_id'],'reviewed_at':t['reviewed_at']}
        row.update(payload);rows.append(row)
    exports['ANSWER_REVIEWS_1200.csv']=csv_bytes(headers,rows)
    # Adjudications 600. Keep true system_id in the official internal export.
    adj_headers=['question_id','system_id','review_system_code','question_en','target_tax_period','generated_answer','actual_context_text','top5_parent_record_ids','context_sha256','prompt_sha256','status','adjudicator_id','adjudication_date']+['final_substantive_correctness','final_temporal_version_correctness','final_completeness_conditions','final_section_subsection_correctness','final_citation_target_correctness','final_citation_entailment','final_actual_context_support','final_unsupported_claims_absent','final_answer_or_abstention_appropriate','final_answer_relevance','final_nugget_coverage_pct','final_citation_present','final_citation_precision','final_citation_recall','final_numeric_values_correct','final_contradiction_free','final_source_reference_checked','final_valid_time_checked','adjudication_notes']
    rows=[]
    for a in all_rows(db,'SELECT * FROM answer_adjudications ORDER BY question_id,system_id'):
        qrow=db.execute('SELECT * FROM questions WHERE question_id=?',(a['question_id'],)).fetchone(); ans=db.execute('SELECT * FROM answers WHERE question_id=? AND system_id=?',(a['question_id'],a['system_id'])).fetchone();p=json.loads(a['payload_json'] or '{}')
        row={'question_id':a['question_id'],'system_id':a['system_id'],'review_system_code':a['review_system_code'],'question_en':qrow['question_en'],'target_tax_period':qrow['target_tax_period'],'generated_answer':ans['generated_answer'] if ans else '', 'actual_context_text':ans['actual_context_text'] if ans else '', 'top5_parent_record_ids':ans['top5_parent_ids'] if ans else '', 'context_sha256':ans['context_sha256'] if ans else '', 'prompt_sha256':ans['prompt_sha256'] if ans else '', 'status':a['status'],'adjudicator_id':a['adjudicator_id'],'adjudication_date':a['adjudication_date']};row.update(p);rows.append(row)
    exports['ANSWER_FINAL_ADJUDICATION_600.csv']=csv_bytes(adj_headers,rows)
    # Retrieval review template with both reviewers and adjudication.
    rrheaders=['question_id','review_system_code','system_id','question_en','target_tax_period_candidate','parent_rank','parent_record_id','valid_time_start','valid_time_end','source_text_excerpt','record_is_correct_statutory_source_REVIEWER_1','record_version_valid_for_target_period_REVIEWER_1','future_amendment_leakage_REVIEWER_1','reviewer_1_id','record_is_correct_statutory_source_REVIEWER_2','record_version_valid_for_target_period_REVIEWER_2','future_amendment_leakage_REVIEWER_2','reviewer_2_id','final_adjudicator_id','final_adjudicated_label','final_review_notes','source_document','title','review_flag']
    rows=[]
    for r in all_rows(db,'SELECT * FROM retrieval_items ORDER BY question_id,system_id,parent_rank'):
        chunk=db.execute('SELECT chunk_text FROM chunks WHERE record_id=? ORDER BY chunk_id LIMIT 1',(r['parent_record_id'],)).fetchone()
        rr={'question_id':r['question_id'],'review_system_code':r['review_system_code'],'system_id':r['system_id'],'question_en':r['question_en'],'target_tax_period_candidate':r['target_tax_period_candidate'],'parent_rank':r['parent_rank'],'parent_record_id':r['parent_record_id'],'valid_time_start':r['valid_time_start'],'valid_time_end':r['valid_time_end'],'source_text_excerpt':chunk['chunk_text'] if chunk else '', 'source_document':r['source_document'],'title':r['title'],'review_flag':r['review_flag']}
        for slot in (1,2):
            rv=db.execute('SELECT * FROM retrieval_review_submissions WHERE item_id=? AND reviewer_slot=?',(r['item_id'],slot)).fetchone()
            rr[f'reviewer_{slot}_id']=rv['reviewer_id'] if rv else ''
            if rv:
                p=json.loads(rv['payload_json']);
                for field in ('record_is_correct_statutory_source','record_version_valid_for_target_period','future_amendment_leakage'):rr[f'{field}_REVIEWER_{slot}']=p.get(field,'')
        adj=db.execute('SELECT * FROM retrieval_adjudications WHERE item_id=?',(r['item_id'],)).fetchone()
        if adj:rr.update({'final_adjudicator_id':adj['final_adjudicator_id'],'final_adjudicated_label':adj['final_label'],'final_review_notes':json.loads(adj['payload_json'] or '{}').get('review_notes','')})
        rows.append(rr)
    exports['RETRIEVAL_VALIDITY_REVIEW_3000.csv']=csv_bytes(rrheaders,rows)
    # Prompt review export
    prheaders=['question_id','prompt_strategy_key','prompt_strategy_name','question_en','question_si','expected_answer_en','expected_answer_si','expected_answer_status','source_reference','top5_parent_ids','generated_answer','cited_record_ids','mean_reference_rougeL_recall_proxy','citation_validity_rate','bilingual_response','review_status','reviewer_id','reviewed_at']+list(PROMPT_FIELDS.keys())
    rows=[]
    for r in all_rows(db,'SELECT * FROM prompt_review_items ORDER BY question_id,strategy_key'):
        x={k:r.get(k,'') for k in prheaders if k not in PROMPT_FIELDS};x.update(json.loads(r['payload_json'] or '{}'));rows.append(x)
    exports['PROMPT_STRATEGY_REVIEW_350.csv']=csv_bytes(prheaders,rows)
    # Focused question-only worksheet for an external tax professional (no model answers exposed).
    question_sheet_headers=['question_id','question_en','target_tax_period','target_as_of_date','answerability_status','expert_answer_en','applicable_section','official_citation','official_source_url','tax_period','as_of_date','valid_time_start','valid_time_end','confidence','professional_role','years_tax_experience','expert_notes']
    question_sheet=[{'question_id':r['question_id'],'question_en':r['question_en'],'target_tax_period':r['target_tax_period'],'target_as_of_date':r['target_as_of_date']} for r in q]
    exports['TAX_EXPERT_QUESTION_SHEET.csv']=csv_bytes(question_sheet_headers,question_sheet)
    # Rebuild the response CSV from saved submissions every time it is exported.
    expert_headers=['question_id','question_en','question_target_tax_period','question_target_as_of_date','reviewer_id','review_slot','submitted_at','answerability_status','expert_answer_en','applicable_section','official_citation','official_source_url','tax_period','as_of_date','valid_time_start','valid_time_end','confidence','professional_role','years_tax_experience','expert_notes']
    expert_rows=[]
    expert_records=all_rows(db,"SELECT g.*,q.question_en,q.target_tax_period AS question_target_tax_period,q.target_as_of_date AS question_target_as_of_date FROM gold_review_submissions g JOIN questions q USING(question_id) ORDER BY g.question_id,g.reviewer_slot")
    for r in expert_records:
        payload=json.loads(r['payload_json'] or '{}')
        if not payload.get('professional_role') and not payload.get('expert_answer_en') and not payload.get('answerability_status'):
            continue
        expert_rows.append({'question_id':r['question_id'],'question_en':r['question_en'],'question_target_tax_period':r['question_target_tax_period'],'question_target_as_of_date':r['question_target_as_of_date'],'reviewer_id':r['reviewer_id'],'review_slot':r['reviewer_slot'],'submitted_at':r['submitted_at'],**{k:payload.get(k,'') for k in ['answerability_status','expert_answer_en','applicable_section','official_citation','official_source_url','tax_period','as_of_date','valid_time_start','valid_time_end','confidence','professional_role','years_tax_experience','expert_notes']}})
    exports['TAX_EXPERT_REVIEW_RESPONSES.csv']=csv_bytes(expert_headers,expert_rows)
    # Expert-facing CSV contains only the question/answer and submitted ratings. The private
    # researcher export retains row-level blind codes so reviews can be joined after review lock.
    answer_expert_public_headers=['question_id','question_en','target_tax_period','target_as_of_date','reviewer_id','review_slot','submitted_at','generated_answer','substantive_correctness','temporal_version_correctness','answer_completeness_conditions','citation_entailment','answer_or_abstention_appropriate','unsupported_claims_present','review_notes']
    answer_expert_internal_headers=['review_row_id','question_id','review_system_code','question_en','target_tax_period','target_as_of_date','reviewer_id','review_slot','submitted_at','generated_answer','substantive_correctness','temporal_version_correctness','answer_completeness_conditions','citation_entailment','answer_or_abstention_appropriate','unsupported_claims_present','review_notes']
    answer_expert_public_rows=[]
    answer_expert_internal_rows=[]
    for t in all_rows(db,"SELECT * FROM answer_review_tasks WHERE status='SUBMITTED' ORDER BY question_id,review_system_code,reviewer_slot"):
        payload=json.loads(t['payload_json'] or '{}')
        if not payload.get('tax_expert_form'):
            continue
        qrow=db.execute('SELECT * FROM questions WHERE question_id=?',(t['question_id'],)).fetchone()
        mapping=db.execute('SELECT value FROM settings WHERE key=?',('private_code_'+t['review_system_code'],)).fetchone()
        sid=mapping['value'] if mapping else CODE_TO_SYSTEM.get(t['review_system_code'],'A_CONVENTIONAL_RAG')
        ans=db.execute('SELECT * FROM answers WHERE question_id=? AND system_id=?',(t['question_id'],sid)).fetchone()
        common={'question_id':t['question_id'],'question_en':qrow['question_en'],'target_tax_period':qrow['target_tax_period'],'target_as_of_date':qrow['target_as_of_date'],'reviewer_id':t['reviewer_id'],'review_slot':t['reviewer_slot'],'submitted_at':t['reviewed_at'],'generated_answer':ans['generated_answer'] if ans else '',**{k:payload.get(k,'') for k in ['substantive_correctness','temporal_version_correctness','answer_completeness_conditions','citation_entailment','answer_or_abstention_appropriate','unsupported_claims_present','review_notes']}}
        answer_expert_public_rows.append(common)
        answer_expert_internal_rows.append({'review_row_id':t['review_row_id'],'review_system_code':t['review_system_code'],**common})
    exports['TAX_EXPERT_ANSWER_ASSESSMENTS.csv']=csv_bytes(answer_expert_public_headers,answer_expert_public_rows)
    exports['TAX_EXPERT_ANSWER_ASSESSMENTS_INTERNAL.csv']=csv_bytes(answer_expert_internal_headers,answer_expert_internal_rows)
    # Preserve actual source run artifacts and source inventory for provenance.
    if SEED_PATH.exists():
        with zipfile.ZipFile(SEED_PATH) as source_zip:
            source_artifacts = {
                'CONTROLLED_ABC_RUN_MANIFEST.json': 'run_manifest.json',
                'PROMPT_STRATEGY_RUN_MANIFEST.json': 'prompt_run_manifest.json',
                'SOURCE_INVENTORY_SHA256.csv': 'source_inventory_sha256.csv',
                'CONTROLLED_ABC_GENERATION_SUMMARY.csv': 'generation_summary.csv',
                'CONTROLLED_ABC_RETRIEVAL_PROXY_METRICS.csv': 'retrieval_proxy_metrics.csv',
                'PROMPT_STRATEGY_DIAGNOSTIC_LEADERBOARD.csv': 'prompt_leaderboard.csv',
                'SCORING_GATE_SOURCE_STATUS.csv': 'scoring_gate_status.csv',
            }
            for export_name, source_name in source_artifacts.items():
                if source_name in source_zip.namelist():
                    exports[export_name] = source_zip.read(source_name)
    # metrics report deliberately preserves pending status; no fabricated accuracy.
    manifest=json.loads(db.execute("SELECT value FROM settings WHERE key='run_manifest'").fetchone()['value'])
    dashboard=dashboard_payload(db)
    metricrows=[]
    metric_defs=[('Answer Accuracy','answer_accuracy','Requires all 200 gold labels approved and all 600 final adjudications.'),('Strict Correct-and-Complete Rate','strict_correct_complete_rate','Requires all 200 gold labels approved and all 600 final adjudications.'),('Correct-Version Retrieval Rate','correct_version_retrieval_rate','Requires all 3,000 retrieval items adjudicated.'),('Future-Amendment Leakage Rate','future_amendment_leakage_rate','Requires all 3,000 retrieval items adjudicated.'),('Answer Review Completion','answer_review_completion','Progress metric; not legal accuracy.'),('Retrieval Double-Review Completion','retrieval_double_review_completion','Progress metric; not legal accuracy.'),('Gold Label Approval','gold_label_approval','Progress metric; not legal accuracy.'),('Prompt Review Completion','prompt_review_completion','Progress metric; not legal accuracy.')]
    for label,key,req in metric_defs:
        val=dashboard.get(key)
        status='FINAL' if val is not None and key in ('answer_accuracy','strict_correct_complete_rate','correct_version_retrieval_rate','future_amendment_leakage_rate') else ('PROGRESS' if key not in ('answer_accuracy','strict_correct_complete_rate','correct_version_retrieval_rate','future_amendment_leakage_rate') else 'PENDING_EXPERT_REVIEW')
        metricrows.append({'metric':label,'value':'' if val is None else val,'status':status,'requirement':req,'run_id':RUN_ID})
    exports['METRICS_REPORT.csv']=csv_bytes(['metric','value','status','requirement','run_id'],metricrows)
    eval_manifest={'run_id':RUN_ID,'generated_at':now_iso(),'experiment_status':manifest.get('status'),'question_count':200,'generation_rows':600,'retrieval_rows':3000,'generator_model':manifest.get('generator_model_id'),'retrieval_mode':manifest.get('retrieval_mode'),'corpus_runtime_parents':manifest.get('shared_candidate_parent_count'),'corpus_runtime_chunks':manifest.get('shared_candidate_chunk_count'),'gold_labels_approved':dashboard['gold_approved'],'answer_reviews_submitted':dashboard['answer_reviews_done'],'answer_reviews_total':dashboard['answer_reviews_total'],'answer_adjudicated':dashboard['answer_adjudicated'],'retrieval_double_reviewed':dashboard['retrieval_double_reviewed'],'retrieval_adjudicated':dashboard['retrieval_adjudicated'],'prompt_reviews_submitted':dashboard['prompt_reviews_done'],'prompt_reviews_total':dashboard['prompt_reviews_total'],'reviewer_protocol':{'gold_labels':'Two independent reviewers; administrator approval required.','answers':'Two independent blind reviews for each question-system answer; adjudication required.','retrieval':'Two independent reviews per Top-5 record; adjudication required.','blind_codes':'Reviewers see SYSTEM_X/Y/Z; mapping is withheld by reviewer endpoints.'},'scoring_rules':{'answer_accuracy':'Computed only after all gold labels are approved and all 600 answer adjudications are complete.','retrieval_metrics':'Computed only after retrieval review adjudication is complete.'},'legal_accuracy_claim_allowed':bool(dashboard['answer_accuracy'] is not None),'seed_data_zip_sha256':hashlib.sha256(SEED_PATH.read_bytes()).hexdigest() if SEED_PATH.exists() else None,'export_file_sha256':{name:hashlib.sha256(data).hexdigest() for name,data in exports.items()},'limitations':manifest.get('limitations',[])}
    exports['EVALUATION_MANIFEST.json']=json.dumps(eval_manifest,indent=2,ensure_ascii=False).encode()

    return exports


def dashboard_payload(db):
    cnt=lambda sql: db.execute(sql).fetchone()[0]
    questions=cnt('SELECT COUNT(*) FROM questions');approved=cnt("SELECT COUNT(*) FROM questions WHERE gold_status='APPROVED'")
    ar_done=cnt("SELECT COUNT(*) FROM answer_review_tasks WHERE status='SUBMITTED'");ar_total=cnt('SELECT COUNT(*) FROM answer_review_tasks')
    adj_done=cnt("SELECT COUNT(*) FROM answer_adjudications WHERE status='ADJUDICATED'");adj_total=cnt('SELECT COUNT(*) FROM answer_adjudications')
    retrieval_total=cnt('SELECT COUNT(*) FROM retrieval_items');double=cnt('SELECT COUNT(*) FROM retrieval_items r WHERE (SELECT COUNT(*) FROM retrieval_review_submissions s WHERE s.item_id=r.item_id)>=2');retrieval_adj=cnt('SELECT COUNT(*) FROM retrieval_adjudications WHERE final_label!=""')
    prompt_done=cnt("SELECT COUNT(*) FROM prompt_review_items WHERE review_status='SUBMITTED'");prompt_total=cnt('SELECT COUNT(*) FROM prompt_review_items')
    gold_double=cnt('SELECT COUNT(*) FROM questions q WHERE (SELECT COUNT(*) FROM gold_review_submissions g WHERE g.question_id=q.question_id)>=2')
    acc=strict=correctv=leak=None
    if approved==questions and adj_done==adj_total:
        ps=[json.loads(r['payload_json'] or '{}') for r in db.execute("SELECT payload_json FROM answer_adjudications WHERE status='ADJUDICATED'")]
        use=[p for p in ps if p.get('final_substantive_correctness') not in ('OUT_OF_SCOPE','UNANSWERABLE','')]
        if use:
            acc=sum(p.get('final_substantive_correctness')=='CORRECT' for p in use)/len(use)
            strict=sum(p.get('final_substantive_correctness')=='CORRECT' and p.get('final_completeness_conditions')=='COMPLETE' and p.get('final_temporal_version_correctness')=='CORRECT' and p.get('final_citation_entailment')=='SUPPORTED' for p in use)/len(use)
    if retrieval_adj==retrieval_total and retrieval_total:
        labs=[r['final_label'] for r in db.execute('SELECT final_label FROM retrieval_adjudications')]
        eligible=[x for x in labs if x not in ('NOT_APPLICABLE','UNCERTAIN','')]
        if eligible:
            correctv=sum(x=='VALID_RETRIEVAL' for x in eligible)/len(eligible)
            leak=sum(x=='FUTURE_AMENDMENT_LEAKAGE' for x in eligible)/len(eligible)
    return {'run_id':RUN_ID,'evaluation_status':'FINAL_ANSWER_METRICS_AVAILABLE' if acc is not None else 'PENDING_EXPERT_ADJUDICATION','questions_total':questions,'gold_approved':approved,'gold_double_reviewed':gold_double,'answer_reviews_done':ar_done,'answer_reviews_total':ar_total,'answer_adjudicated':adj_done,'answer_adjudication_total':adj_total,'retrieval_double_reviewed':double,'retrieval_total':retrieval_total,'retrieval_adjudicated':retrieval_adj,'prompt_reviews_done':prompt_done,'prompt_reviews_total':prompt_total,'answer_accuracy':acc,'strict_correct_complete_rate':strict,'correct_version_retrieval_rate':correctv,'future_amendment_leakage_rate':leak,'answer_review_completion':ar_done/ar_total if ar_total else 0,'retrieval_double_review_completion':double/retrieval_total if retrieval_total else 0,'gold_label_approval':approved/questions if questions else 0,'prompt_review_completion':prompt_done/prompt_total if prompt_total else 0,'generation_count':600,'generation_errors':0,'prototype_notice':'Legal accuracy is withheld until approved gold labels and final adjudication are complete.'}

@app.get('/api/dashboard')
def api_dashboard():
    if session.get('role') == 'tax_expert':
        return jsonify({'error':'The tax-expert workspace does not expose research dashboards.'}),403
    with get_db() as db:return jsonify(dashboard_payload(db))

@app.get('/api/export/bundle.zip')
@admin_required
def export_bundle():
    with get_db() as db: exports=build_exports(db)
    bio=io.BytesIO()
    with zipfile.ZipFile(bio,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,data in exports.items(): z.writestr(name,data)
        # Include pending draft evaluation queues so work can continue outside the app too.
        z.writestr('README_EXPORT_STATUS.txt','This export contains current research review state. Files named APPROVED contain approved rows only. A zero-row approved gold file means approval is still pending. The metrics report keeps legal accuracy blank until its scoring gates are met.\n')
    bio.seek(0);return send_file(bio,mimetype='application/zip',as_attachment=True,download_name=f'tax_research_evaluation_export_{RUN_ID}.zip')

@app.get('/api/export/<name>')
@admin_required
def export_one(name):
    with get_db() as db: exports=build_exports(db)
    if name not in exports: abort(404)
    mime='application/json' if name.endswith('.json') else 'text/csv; charset=utf-8'
    return send_file(io.BytesIO(exports[name]),mimetype=mime,as_attachment=True,download_name=name)

@app.errorhandler(401)
def unauthorized(_):
    if request.path.startswith('/api/'):return jsonify({'error':'Authentication required.'}),401
    return render_template('public_index.html'),200
@app.errorhandler(403)
def forbidden(_):
    if request.path.startswith('/api/'):return jsonify({'error':'Administrator access is required.'}),403
    return render_template('public_index.html'),200

# Initialize on import for Flask CLI and WSGI deployment.
init_db()

if __name__ == '__main__':
    host=os.environ.get('HOST','127.0.0.1');port=int(os.environ.get('PORT','5000'))
    print('Tax Research Assistant prototype')
    print('Researcher login code: set REVIEWER_ACCESS_CODE; local default is review-demo-change-me')
    print('Tax expert login code: set TAX_EXPERT_ACCESS_CODE; local default is tax-expert-demo-change-me')
    print('Administrator login code: set ADMIN_ACCESS_CODE; local default is admin-demo-change-me')
    print('For external exposure, change both codes and FLASK_SECRET_KEY. This is a research prototype.')
    print(f'Starting server at http://{host}:{port}')
    try:
        app.run(host=host,port=port,debug=os.environ.get('FLASK_DEBUG','0')=='1',threaded=True)
    except OSError as e:
        if getattr(e, 'errno', None) == 48 and port == 5000:
            fallback_port = 5001
            print(f'Port 5000 is already in use (e.g. macOS AirPlay / ControlCenter). Falling back to http://{host}:{fallback_port}')
            app.run(host=host,port=fallback_port,debug=os.environ.get('FLASK_DEBUG','0')=='1',threaded=True)
        else:
            raise
