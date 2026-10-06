import threading


def test_batch_tag_operations(library, tmp_path):
    paths = [str(tmp_path / f'img_{i}.jpg') for i in range(3)]
    library.add_tags(paths, ['cat', 'cute'])
    for path in paths:
        assert set(library.get_tags(path)) == {'cat', 'cute'}
    assert {tag['name']: tag['count'] for tag in library.get_tag_usage()} == {'cat': 3, 'cute': 3}
    library.remove_tags([paths[0]], ['cute'])
    assert library.get_tags(paths[0]) == ['cat']
    library.rename_tag('cat', 'feline')
    library.add_tags([paths[0]], ['a', 'b'])
    library.merge_tags(['a', 'b'], 'ab')
    assert set(library.get_tags(paths[0])) == {'feline', 'ab'}
    assert 'cat' not in library.get_all_tags()


def test_batch_tags_reentrant_lock_does_not_deadlock(library):
    errors = []

    def add():
        try:
            with library._lock:
                library.add_tags(['one.jpg', 'two.jpg'], ['test'])
        except Exception as error:
            errors.append(error)
        finally:
            library.close_thread_connection()

    thread = threading.Thread(target=add, daemon=True)
    thread.start()
    thread.join(timeout=3)
    assert not thread.is_alive(), 'batch tags deadlocked'
    assert not errors
    assert library.get_tags('two.jpg') == ['test']
