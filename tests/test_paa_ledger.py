from paa_ledger import Ledger, LedgerItem, make_item_id, now_iso


def _make_item(project='alpha', item_type='context-drift', evidence='foo.py'):
    item_id = make_item_id(item_type, project, evidence)
    return LedgerItem(
        id=item_id, type=item_type, project=project,
        project_path=f'/tmp/{project}', summary=f'test {item_type}',
        evidence=evidence, severity='warning', created=now_iso(),
    )


def test_make_item_id_deterministic():
    a = make_item_id('context-drift', 'alpha', 'foo.py')
    b = make_item_id('context-drift', 'alpha', 'foo.py')
    assert a == b
    assert len(a) == 16


def test_make_item_id_differs():
    a = make_item_id('context-drift', 'alpha', 'foo.py')
    b = make_item_id('context-drift', 'beta', 'foo.py')
    assert a != b


def test_add_if_new():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    item = _make_item()
    assert ledger.add_if_new(item) is True
    assert ledger.add_if_new(item) is False  # duplicate


def test_add_skips_dismissed():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    item = _make_item()
    ledger.add_if_new(item)
    ledger.update_status(item.id, 'dismissed')
    item2 = _make_item()  # same fingerprint
    assert ledger.add_if_new(item2) is False


def test_add_replaces_resolved():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    item = _make_item()
    ledger.add_if_new(item)
    ledger.update_status(item.id, 'resolved')
    item2 = _make_item()  # same fingerprint, was resolved — should re-add
    assert ledger.add_if_new(item2) is True


def test_pending_items():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    ledger.add_if_new(_make_item(project='a'))
    ledger.add_if_new(_make_item(project='b'))
    ledger.update_status(
        make_item_id('context-drift', 'a', 'foo.py'), 'dismissed',
    )
    assert len(ledger.pending_items()) == 1


def test_pending_count():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    assert ledger.pending_count == 0
    ledger.add_if_new(_make_item())
    assert ledger.pending_count == 1


def test_sweep_resolves_stale():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    item = _make_item()
    ledger.add_if_new(item)
    ledger.sweep(set())  # nothing active — pending item is stale
    items = ledger.pending_items()
    assert len(items) == 0
    assert ledger._items[item.id].status == 'resolved'


def test_sweep_keeps_active():
    ledger = Ledger(path='/tmp/nonexistent_ledger.json')
    item = _make_item()
    ledger.add_if_new(item)
    ledger.sweep({item.id})  # item still detected
    assert ledger._items[item.id].status == 'pending'


def test_save_and_load(tmp_path):
    path = str(tmp_path / 'ledger.json')
    ledger = Ledger(path=path)
    ledger.add_if_new(_make_item(project='alpha'))
    ledger.add_if_new(_make_item(project='beta'))
    ledger.save()

    ledger2 = Ledger(path=path)
    ledger2.load()
    assert ledger2.pending_count == 2


def test_load_missing_file():
    ledger = Ledger(path='/tmp/definitely_missing_12345.json')
    ledger.load()  # should not raise
    assert ledger.pending_count == 0


def test_load_corrupt_json(tmp_path):
    path = tmp_path / 'ledger.json'
    path.write_text('not json!')
    ledger = Ledger(path=str(path))
    ledger.load()  # should not raise
    assert ledger.pending_count == 0


def test_save_atomic_no_temp_files(tmp_path):
    path = str(tmp_path / 'ledger.json')
    ledger = Ledger(path=path)
    ledger.add_if_new(_make_item())
    ledger.save()
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == 'ledger.json'
