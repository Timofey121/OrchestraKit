from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec('orchestra_kit.usage'))
        from orchestra_kit.usage import summarize_usage
        self.summarize = summarize_usage
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def log(self, name: str, items: list[dict]) -> Path:
        path = self.root / name
        path.write_text(''.join(json.dumps(item) + '\n' for item in items))
        return path

    def record(self, response='r1', input_tokens=100, cached=80, output=20, thread='root'):
        return {'type': 'token_usage_record', 'payload': {
            'thread_id': thread, 'response_id': response,
            'usage': {'input_tokens': input_tokens, 'cached_input_tokens': cached,
                      'output_tokens': output, 'reasoning_output_tokens': 5,
                      'cache_write_input_tokens': 0, 'total_tokens': input_tokens + output}}}

    def headers(self, model='gpt-example', thread='root'):
        return [{'type': 'session_meta', 'payload': {'id': thread}},
                {'type': 'turn_context', 'payload': {'model': model, 'effort': 'high'}}]

    def test_records_are_not_added_to_cumulative_token_events(self) -> None:
        record = self.record()
        event = {'type': 'event_msg', 'payload': {'type': 'token_count',
                 'info': {'total_token_usage': record['payload']['usage']}}}
        path = self.log('root.jsonl', self.headers() + [record, event])
        result = self.summarize([path])
        self.assertEqual(result['totals']['input_tokens'], 100)
        self.assertEqual(result['totals']['uncached_input_tokens'], 20)
        self.assertEqual(result['totals']['total_tokens'], 120)
        self.assertEqual(result['response_count'], 1)
        self.assertEqual(result['models'][0]['model'], 'gpt-example')
        self.assertEqual(result['models'][0]['model_source'], 'turn_context (configured, not runtime-attested)')

    def test_models_and_efforts_are_grouped_per_response_not_last_context(self) -> None:
        path = self.log('root.jsonl', self.headers('cheap') + [self.record(),
            {'type': 'turn_context', 'payload': {'model': 'strong', 'effort': 'medium'}},
            self.record('r2', 200, 150, 30)])
        result = self.summarize([path])
        self.assertEqual({row['model'] for row in result['models']}, {'cheap', 'strong'})
        self.assertEqual(result['totals']['input_tokens'], 300)
        self.assertEqual(result['totals']['uncached_input_tokens'], 70)

    def test_response_ids_deduplicate_parent_and_child_logs_and_resolve_child_model(self) -> None:
        record = self.record(thread='child')
        parent = self.log('parent.jsonl', self.headers('root-model') + [record])
        child = self.log('child.jsonl', self.headers('leaf-model', 'child') + [record])
        result = self.summarize([parent, child, child])
        self.assertEqual(result['response_count'], 1)
        self.assertEqual(result['totals']['total_tokens'], 120)
        self.assertEqual(result['models'][0]['model'], 'leaf-model')

    def test_child_without_its_context_is_never_labeled_as_parent_model(self) -> None:
        path = self.log('parent.jsonl', self.headers('root-model') + [self.record(thread='child')])
        self.assertIsNone(self.summarize([path])['models'][0]['model'])

    def test_cumulative_only_legacy_logs_use_last_event_not_sum(self) -> None:
        events=[]
        for inp in [100, 200]:
            record = self.record(input_tokens=inp)
            events.append({'type': 'event_msg', 'payload': {'type': 'token_count',
                'info': {'total_token_usage': record['payload']['usage']}}})
        path = self.log('legacy.jsonl', self.headers() + events)
        result = self.summarize([path])
        self.assertEqual(result['totals']['input_tokens'], 200)
        self.assertIsNone(result['response_count'])
        self.assertTrue(result['warnings'])

    def test_unknown_counters_remain_null_and_no_price_is_invented(self) -> None:
        record=self.record()
        del record['payload']['usage']['cached_input_tokens']
        path=self.log('root.jsonl', self.headers() + [record])
        result=self.summarize([path])
        self.assertIsNone(result['totals']['cached_input_tokens'])
        self.assertIsNone(result['totals']['uncached_input_tokens'])
        self.assertNotIn('cost_usd', result['totals'])

    def test_inconsistent_or_negative_counters_are_rejected(self) -> None:
        for name, value in [('input_tokens', -1), ('cached_input_tokens', 101),
                            ('output_tokens', True), ('reasoning_output_tokens', 21),
                            ('total_tokens', 999)]:
            record=self.record()
            record['payload']['usage'][name]=value
            path=self.log('bad.jsonl', self.headers() + [record])
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.summarize([path])

    def test_duplicate_response_with_conflicting_usage_is_refused(self) -> None:
        path=self.log('root.jsonl', self.headers() + [self.record(), self.record(input_tokens=101)])
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.summarize([path])

    def test_partial_log_and_empty_log_do_not_claim_complete_usage(self) -> None:
        path=self.log('root.jsonl', self.headers() + [self.record()])
        path.write_text(path.read_text() + '{broken')
        result=self.summarize([path])
        self.assertFalse(result['complete'])
        self.assertTrue(result['warnings'])
        empty=self.log('empty.jsonl', [])
        with self.assertRaisesRegex(ValueError, 'usage'):
            self.summarize([empty])

    def test_ephemeral_cli_turns_sum_without_inventing_model_or_response_count(self) -> None:
        events = [{'type': 'thread.started', 'thread_id': 'root'}]
        for inp in [100, 200]:
            events.append({'type': 'turn.completed', 'usage': self.record(input_tokens=inp)['payload']['usage']})
        path = self.log('cli.jsonl', events)
        result = self.summarize([path, path])
        self.assertEqual(result['totals']['input_tokens'], 300)
        self.assertIsNone(result['response_count'])
        self.assertIsNone(result['models'][0]['model'])
        self.assertTrue(any('CLI' in warning for warning in result['warnings']))

    def test_cli_turns_are_not_added_to_native_records_or_legacy_cumulative(self) -> None:
        usage = self.record()['payload']['usage']
        cli = {'type': 'turn.completed', 'usage': usage}
        legacy = {'type': 'event_msg', 'payload': {'type': 'token_count',
                  'info': {'total_token_usage': usage}}}
        path = self.log('mixed.jsonl', self.headers() + [self.record(), cli, legacy])
        self.assertEqual(self.summarize([path])['totals']['input_tokens'], 100)
        path = self.log('fallback.jsonl', [cli, legacy])
        self.assertEqual(self.summarize([path])['totals']['input_tokens'], 100)


if __name__ == '__main__':
    unittest.main()
