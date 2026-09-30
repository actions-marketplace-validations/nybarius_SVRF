"""Local explanations retain earlier groups from the current automatic round."""
import json

from svrf import cli
from svrf.config import from_dict


def setup_round(tmp_path):
    config = from_dict({'repo': 'example/project', 'state_dir': str(tmp_path),
                        'gate': {'commands': ['true']}})
    directory = tmp_path / 'receipts'
    directory.mkdir()
    first, last = [directory / f'train-20260930T000000Z-1-{n:04d}.json' for n in (0, 1)]
    first.write_text(json.dumps({'merges': [{'number': 2, 'identity': True}],
                                 'out': {'3': {'with': [2], 'paths': ['shared.py']}}, 'families': []}))
    last.write_text(json.dumps({'merges': [{'number': 1, 'identity': True}], 'out': {}, 'families': []}))
    state = {'held': {}, 'last_tick': {'receipt': str(last), 'receipts': [str(first), str(last)]}}
    (tmp_path / 'state.json').write_text(json.dumps(state))
    return config, first, last


def test_why_reports_an_earlier_groups_verified_merge(tmp_path):
    config, first, last = setup_round(tmp_path)
    assert cli.why(config, 2)['state'] == 'MERGED'
    assert cli.why(config, 1)['state'] == 'MERGED'


def test_why_reports_an_earlier_groups_pair_conflict(tmp_path):
    config, first, last = setup_round(tmp_path)
    result = cli.why(config, 3)
    assert result['state'] == 'OUT_THIS_ROUND'
    assert result['detail']['paths'] == ['shared.py']


def test_missing_group_receipt_cannot_borrow_an_older_rounds_result(tmp_path):
    config, first, last = setup_round(tmp_path)
    old = first.with_name('train-20260929T000000Z-1-0000.json')
    old.write_text(first.read_text())
    first.unlink()
    assert cli.why(config, 2)['state'] == 'UNKNOWN'


def test_later_explicit_train_remains_the_latest_round(tmp_path):
    config, first, last = setup_round(tmp_path)
    later = first.with_name('train-20260930T000001Z-1-0002.json')
    later.write_text(json.dumps({'merges': [{'number': 9, 'identity': True}], 'out': {}, 'families': []}))
    assert cli.why(config, 9)['state'] == 'MERGED'
    assert cli.why(config, 2)['state'] == 'UNKNOWN'
