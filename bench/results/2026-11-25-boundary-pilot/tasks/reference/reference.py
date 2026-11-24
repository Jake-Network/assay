"""Reference solutions for the boundary pilot tasks: python reference.py CHECKOUT TASK PHASE [dead-end]."""
import re
import sys
from pathlib import Path

repo, task, phase = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
dead_end = len(sys.argv) > 4


def add(path, code):
    p = repo / path
    p.write_text(p.read_text(encoding="utf-8").rstrip("\n") + "\n\n\n" + code.strip("\n") + "\n", encoding="utf-8")


def all_add(path, name, tuple_=False):
    p = repo / path
    text = p.read_text(encoding="utf-8")
    text, n = re.subn(r"__all__ = ([(\[])", lambda m: f"__all__ = {m.group(1)}'{name}', ", text, count=1)
    assert n == 1, path
    p.write_text(text, encoding="utf-8")


def curried(line):
    p = repo / "toolz/curried/__init__.py"
    text = p.read_text(encoding="utf-8")
    assert text.count("\ndel exceptions\n") == 1
    p.write_text(text.replace("\ndel exceptions\n", "\n" + line + "\n\ndel exceptions\n"), encoding="utf-8")


DEEP_V1 = '''
def deep_merge(*dicts, **kwargs):
    """Merge dictionaries recursively; later values win unless both are mappings."""
    from collections.abc import Mapping
    if len(dicts) == 1 and not isinstance(dicts[0], Mapping):
        dicts = dicts[0]
    factory = _get_factory(deep_merge, kwargs)

    def go(ds):
        rv = factory()
        for d in ds:
            for k, v in d.items():
                if isinstance(v, Mapping):
                    prev = rv.get(k)
                    rv[k] = go([prev, v] if isinstance(prev, Mapping) else [v])
                else:
                    rv[k] = v
        return rv
    return go(dicts)
'''
DEEP_V2 = '''
def deep_merge(*dicts, **kwargs):
    """Merge dictionaries recursively; later values win unless both are mappings (or lists, with concat)."""
    from collections.abc import Mapping
    strategy = kwargs.pop('list_strategy', 'replace')
    if strategy not in ('replace', 'concat'):
        raise ValueError('list_strategy must be "replace" or "concat"')
    if len(dicts) == 1 and not isinstance(dicts[0], Mapping):
        dicts = dicts[0]
    factory = _get_factory(deep_merge, kwargs)

    def go(ds):
        rv = factory()
        for d in ds:
            for k, v in d.items():
                prev = rv.get(k)
                if isinstance(v, Mapping):
                    rv[k] = go([prev, v] if isinstance(prev, Mapping) else [v])
                elif strategy == 'concat' and isinstance(v, list):
                    rv[k] = (prev if isinstance(prev, list) else []) + list(v)
                else:
                    rv[k] = v
        return rv
    return go(dicts)
'''
DEEP_DEAD = '''
def deep_merge(*dicts, **kwargs):
    return merge_with(deep_merge, *dicts, **kwargs)
'''

if task == "toolz" and phase == 1:
    add("toolz/dicttoolz.py", DEEP_V1)
    all_add("toolz/dicttoolz.py", "deep_merge")
    add("toolz/itertoolz.py", '''
def split_when(pred, seq):
    """Split seq into lists of consecutive items, starting a new list where pred(prev, item) is true."""
    it = iter(seq)
    try:
        prev = next(it)
    except StopIteration:
        return
    chunk = [prev]
    for item in it:
        if pred(prev, item):
            yield chunk
            chunk = []
        chunk.append(item)
        prev = item
    yield chunk
''')
    all_add("toolz/itertoolz.py", "split_when")
    curried("deep_merge = toolz.deep_merge\nsplit_when = toolz.curry(toolz.split_when)")
elif task == "toolz" and phase == 2 and dead_end:
    add("toolz/dicttoolz.py", DEEP_DEAD)
elif task == "toolz" and phase == 2:
    add("toolz/dicttoolz.py", DEEP_V2)
    add("toolz/recipes.py", '''
def chunk_by_size(seq, max_size, size=len):
    """Lazily group consecutive items into lists whose total size stays at most max_size."""
    if max_size <= 0:
        raise ValueError('max_size must be positive')

    def go():
        chunk, total = [], 0
        for item in seq:
            n = size(item)
            if chunk and total + n > max_size:
                yield chunk
                chunk, total = [], 0
            chunk.append(item)
            total += n
            if total > max_size:
                yield chunk
                chunk, total = [], 0
        if chunk:
            yield chunk
    return go()
''')
    all_add("toolz/recipes.py", "chunk_by_size")
    curried("chunk_by_size = toolz.curry(toolz.chunk_by_size)")
elif task == "boltons" and phase == 1:
    add("boltons/strutils.py", '''
_camel_word_re = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+')


def split_camel(text):
    """Split a camelCase or PascalCase string into its words."""
    return _camel_word_re.findall(text)
''')
    all_add("boltons/strutils.py", "split_camel")
    add("boltons/iterutils.py", '''
def run_length(iterable, key=None):
    """Return (first_item, count) for each run of consecutive items with equal keys."""
    runs, last = [], None
    for item in iterable:
        k = item if key is None else key(item)
        if runs and k == last:
            runs[-1] = (runs[-1][0], runs[-1][1] + 1)
        else:
            runs.append((item, 1))
            last = k
    return runs
''')
elif task == "boltons" and phase == 2 and dead_end:
    add("boltons/strutils.py", '''
def split_camel(text):
    return re.findall(r"[A-Z][a-z]*", text)
''')
elif task == "boltons" and phase == 2:
    add("boltons/mathutils.py", '''
def weighted_median(values, weights):
    """The smallest value whose cumulative weight reaches half the total weight."""
    values, weights = list(values), list(weights)
    if len(values) != len(weights) or any(w < 0 for w in weights):
        raise ValueError('values and weights must have equal lengths and weights must be nonnegative')
    total = sum(weights)
    if total <= 0:
        raise ValueError('the total weight must be positive')
    cumulative = 0
    for value, weight in sorted((v, w) for v, w in zip(values, weights) if w > 0):
        cumulative += weight
        if cumulative * 2 >= total:
            return value
''')
    add("boltons/strutils.py", '''
def camel_to_kebab(text):
    """Lowercase the words of split_camel(text) and join them with hyphens."""
    return '-'.join(word.lower() for word in split_camel(text))
''')
    all_add("boltons/strutils.py", "camel_to_kebab")
else:
    raise SystemExit("unknown task or phase")
