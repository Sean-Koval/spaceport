"""Read real legacy defaults for extracted shell probes; never runtime evidence."""
import re
import shlex


def legacy_initialization(source):
    lines=[]
    for name in ('PARTITION_MODE','GATE_PYTHON'):
        matches=re.findall(r'^'+name+r'=.*$',source,re.M)
        assert len(matches)==1, 'Exactly one top-level shell default is required'
        lines.extend(matches)
    return '\n'.join(lines)+'\n'


def expanded_legacy_source(source):
    initialization=legacy_initialization(source)
    value=re.search(r'^GATE_PYTHON=\((.*)\)$',initialization,re.M)
    assert value, 'A closed literal command array is required'
    arguments=shlex.split(value[1])
    assert all(re.fullmatch(r'[A-Za-z0-9_-]+',part) for part in arguments)
    return source.replace('"${GATE_PYTHON[@]}"',shlex.join(arguments))
