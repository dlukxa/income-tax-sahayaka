"""Route smoke tests. Run after installing requirements: python -m unittest discover -s tests"""
import csv
import io
import os
import tempfile
import unittest
from pathlib import Path

import app as research_app


class ResearchAppSmokeTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = research_app.DB_PATH
        self.tempdir = tempfile.TemporaryDirectory(prefix='tax-research-test-')
        research_app.DB_PATH = Path(self.tempdir.name) / 'test.sqlite3'
        research_app.init_db()
        self.client = research_app.app.test_client()

    def tearDown(self):
        research_app.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_homepage(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Income Tax Sahayaka', r.data)
        rc = self.client.get('/research-console')
        self.assertEqual(rc.status_code, 200)
        self.assertIn(b'Tax expert / accountant', rc.data)

    def test_seed_status(self):
        r = self.client.get('/api/status')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['questions'], 200)
        self.assertEqual(d['answers'], 600)
        self.assertEqual(d['retrieval_records'], 3000)

    def test_dashboard_withholds_accuracy(self):
        r = self.client.get('/api/dashboard')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIsNone(d['answer_accuracy'])
        self.assertEqual(d['answer_adjudication_total'], 600)

    def test_exact_question_returns_archived_results(self):
        question = 'On what date did the Inland Revenue Act, No. 24 of 2017 come into operation?'
        r = self.client.post('/api/query/stream', json={'question': question, 'legal_date': '2025-03-31', 'mode': 'compare'})
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn('archived_controlled_run', body)
        self.assertIn('Q001', body)

    def test_regular_reviewer_blind_code(self):
        r = self.client.post('/api/login', json={'reviewer_id': 'reviewer_01', 'role': 'reviewer', 'access_code': os.environ.get('REVIEWER_ACCESS_CODE', 'review-demo-change-me')})
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/api/review/retrieval/next?slot=1')
        self.assertEqual(r.status_code, 200)
        task = r.get_json()['task']
        self.assertNotIn('system_id', task)

    def test_tax_expert_only_sees_question_and_can_save_csv_response(self):
        code = os.environ.get('TAX_EXPERT_ACCESS_CODE', 'tax-expert-demo-change-me')
        r = self.client.post('/api/login', json={'reviewer_id': 'tax_expert_01', 'role': 'tax_expert', 'access_code': code})
        self.assertEqual(r.status_code, 200)
        task_response = self.client.get('/api/tax-expert/gold/next')
        self.assertEqual(task_response.status_code, 200)
        data = task_response.get_json()
        task = data['task']
        self.assertIsNotNone(task)
        self.assertEqual(set(task.keys()), {'question_id', 'question_en', 'target_tax_period', 'target_as_of_date'})
        self.assertNotIn('system_id', task)
        self.assertNotIn('generated_answer', task)
        self.assertNotIn('actual_context_text', task)

        payload = {
            'question_id': task['question_id'],
            'assigned_slot': data['assigned_slot'],
            'answerability_status': 'ANSWERABLE',
            'confidence': 'HIGH',
            'professional_role': 'ACCOUNTANT',
            'years_tax_experience': '8',
            'expert_answer_en': 'Test expert reference answer for application validation.',
            'applicable_section': 'Section 2',
            'official_citation': 'Inland Revenue Act, No. 24 of 2017, section 2',
            'official_source_url': 'https://www.ird.gov.lk/en/publications/sitepages/acts.aspx',
            'tax_period': task.get('target_tax_period') or 'Test period',
            'as_of_date': task.get('target_as_of_date') or '2025-03-31',
            'valid_time_start': '2018-04-01',
            'valid_time_end': '',
            'expert_notes': 'Smoke test only.'
        }
        saved = self.client.post('/api/tax-expert/gold/submit', json=payload)
        self.assertEqual(saved.status_code, 200, saved.get_json())
        self.assertEqual(saved.get_json()['done'], 1)

        response_csv = self.client.get('/api/tax-expert/export.csv')
        self.assertEqual(response_csv.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response_csv.get_data(as_text=True).lstrip('\ufeff'))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reviewer_id'], 'tax_expert_01')
        self.assertEqual(rows[0]['expert_answer_en'], payload['expert_answer_en'])
        self.assertEqual(rows[0]['applicable_section'], 'Section 2')

    def test_tax_expert_answer_review_is_blinded_and_own_csv_hides_internal_keys(self):
        code = os.environ.get('TAX_EXPERT_ACCESS_CODE', 'tax-expert-demo-change-me')
        login = self.client.post('/api/login', json={'reviewer_id': 'tax_expert_answer_01', 'role': 'tax_expert', 'access_code': code})
        self.assertEqual(login.status_code, 200)
        response = self.client.get('/api/tax-expert/answer/next')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        task = data['task']
        self.assertIsNotNone(task)
        self.assertNotIn('system_id', task)
        self.assertNotIn('review_system_code', task)
        self.assertNotIn('actual_context_text', task)
        self.assertNotIn('top5_parent_ids', task)
        self.assertIn('generated_answer', task)

        submission = {
            'review_row_id': task['review_row_id'],
            'substantive_correctness': 'UNCLEAR',
            'temporal_version_correctness': 'UNCLEAR',
            'answer_completeness_conditions': 'PARTIALLY_COMPLETE',
            'citation_entailment': 'SUPPORTED',
            'answer_or_abstention_appropriate': 'UNCLEAR',
            'unsupported_claims_present': 'UNCLEAR',
            'review_notes': 'Smoke test only; not an expert legal judgment.'
        }
        saved = self.client.post('/api/tax-expert/answer/submit', json=submission)
        self.assertEqual(saved.status_code, 200, saved.get_json())

        exported = self.client.get('/api/tax-expert/answer-assessments.csv')
        self.assertEqual(exported.status_code, 200)
        text = exported.get_data(as_text=True).lstrip('\ufeff')
        self.assertNotIn('review_system_code', text)
        self.assertNotIn('SYSTEM_X', text)
        self.assertNotIn('review_row_id', text)
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['question_id'], task['question_id'])
        self.assertEqual(rows[0]['substantive_correctness'], 'UNCLEAR')


    def test_public_homepage_does_not_embed_external_survey_or_research_counts(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'What do you need to know?', response.data)
        self.assertNotIn(b'External expert trial', response.data)
        self.assertNotIn(b'200</span>', response.data)
        self.assertNotIn(b'EXPERT_QUERY_ANSWER_REVIEWS', response.data)

    def test_internal_research_console_is_separate_route(self):
        response = self.client.get('/research-console')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Expert workspace', response.data)
        self.assertIn(b'Evaluation', response.data)

    def test_external_evaluation_form_is_not_embedded_in_main_app(self):
        response = self.client.get('/expert')
        self.assertEqual(response.status_code, 404)

        # Even a stale public_expert request flag must not create a survey session or
        # add survey identifiers to the normal query result.
        question = 'On what date did the Inland Revenue Act, No. 24 of 2017 come into operation?'
        response = self.client.post('/api/query/stream', json={
            'question': question, 'legal_date': '2025-03-31', 'mode': 'compare', 'public_expert': True
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('expert_query_id', response.get_data(as_text=True))

    def test_system_results_use_full_names(self):
        question = 'On what date did the Inland Revenue Act, No. 24 of 2017 come into operation?'
        response = self.client.post('/api/query/stream', json={'question': question, 'legal_date': '2025-03-31', 'mode': 'compare'})
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Conventional RAG', body)
        self.assertIn('Basic Time-Filtered RAG', body)
        self.assertIn('Amendment-Aware Temporal RAG', body)

    def test_tax_expert_cannot_access_internal_review_queues_or_dashboard(self):
        code = os.environ.get('TAX_EXPERT_ACCESS_CODE', 'tax-expert-demo-change-me')
        self.client.post('/api/login', json={'reviewer_id': 'tax_expert_02', 'role': 'tax_expert', 'access_code': code})
        self.assertEqual(self.client.get('/api/review/answer/next?slot=1').status_code, 403)
        self.assertEqual(self.client.get('/api/dashboard').status_code, 403)


if __name__ == '__main__':
    unittest.main()
