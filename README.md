# Tax Research Assistant — Research Web App

A Flask-based Sri Lankan personal income tax research prototype. The main app displays saved controlled-run answers for exact benchmark questions and retrieves local legal evidence for other questions. The public-facing query workspace is separate from the expert evaluation survey.

## Separation of concerns

- This package contains the tax-question web app and the internal reviewer/administrator research workspace.
- The no-login external expert evaluation page and its `EXPERT_*` survey exports have been removed from this web app. Use the separately packaged **Tax Expert Evaluation Form** for the post-use questionnaire.
- The main app's fixed-benchmark/retrieval review queues and their protected exports remain in the internal research console. They are distinct from the post-use survey.

## Important behavior: saved answers versus new questions

1. If the submitted text exactly matches one of the 200 saved benchmark questions, the app displays the actual archived system answers and evidence from the controlled run. These outputs are not rerun, and changing the date input does not change the archived answer.
2. For a new question, the app searches the bundled corpus and returns evidence candidates. It does not generate an answer unless `RAG_API_URL` is configured to a live backend that returns usable answers for the requested system(s).
3. Records without verified validity intervals are shown with uncertainty rather than assumed valid.

## Included capabilities

- Question page with legal-date input and selection of one system or all three.
- Full system names: Conventional RAG, Basic Time-Filtered RAG, and Amendment-Aware Temporal RAG.
- Actual archived answers and Top-5 evidence for exact questions in the saved controlled run.
- Local evidence search over bundled runtime corpus chunks for new questions.
- Login-protected internal review queues for answer review, gold labels, source validity, prompt outputs, adjudication, dashboard, and exports.
- Fixed benchmark/retrieval exports for research analysis.

## Run locally (macOS/Linux)

Python 3.10+ is recommended.

```bash
cd tax_temporal_research_app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and replace example secrets. Then run:

```bash
set -a
source .env
set +a
python app.py
```

Open <http://127.0.0.1:5000> for the public query interface. The internal review console is at <http://127.0.0.1:5000/research-console> and requires a reviewer/admin access code to use its protected actions. On Windows, use `run_windows.bat`.

## Internal reviewer/admin access

For a local demonstration, defaults are `review-demo-change-me` for reviewers and `admin-demo-change-me` for administrators. Replace these before deployment. This is a research prototype; do not deploy with example access codes or the development secret.

## Live RAG backend (required for generation on arbitrary questions)

Set `RAG_API_URL` to a trusted API endpoint implementing this request contract:

```json
{
  "question": "What rule applied in the 2022/2023 year of assessment?",
  "as_of_date": "2025-03-31",
  "tax_period": "Year of assessment 2022/2023",
  "systems": [
    "A_CONVENTIONAL_RAG",
    "B_BASIC_TIME_FILTERED_RAG",
    "C_AMENDMENT_AWARE_TEMPORAL_RAG"
  ]
}
```

The response should include a `systems` list with each requested `system_id`, an `answer` field, and source evidence where available. Protect the endpoint with authentication and HTTPS. Never expose model tokens in browser JavaScript.

## Research integrity and limitations

This is a local research prototype, not a production tax service. Evidence preview is not an AI-generated answer and must not be counted as an answer-accuracy observation. Automated retrieval diagnostics are not legal answer accuracy. The fixed benchmark's legal-accuracy metrics remain gated on approved reference labels and answer adjudication. The post-use tax-expert questionnaire is a separate artifact; its subjective ratings should be described as expert-perceived quality, not as gold-standard benchmark accuracy.

## Project structure

```text
app.py                Flask routes, research logic, protected review queues/exports
templates/public_index.html       Public tax-law query interface
templates/research_console.html   Internal reviewer/admin console
static/public.css                 Public interface styles
static/public.js                  Public query/result handling
static/app.js                     Internal review console logic
data/seed_data.zip    Controlled run outputs, queues, and corpus records
```
